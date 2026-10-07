"""The prober: checks the fleet's broker from outside the broker path and
raises `BrokerUnreachable` through the alerter's store.

The alerter and the houses share one broker, so a broker outage is
silent on the broker path: the alerter hears nothing and sees every
house as silent at once. The prober is a detector that reads no broker.
Once an interval it checks the two doors the fleet uses, as a client of
each: an AMQPS round trip (connect, declare an exclusive auto-delete
queue, publish one message to it through the default exchange, receive
it back; a connect-only check passes through a memory or disk alarm that
blocks every publisher) and an MQTT connect on the TLS listener the
scadas use (TLS handshake, CONNECT, CONNACK; any CONNACK means the MQTT
plugin and its listener answered). The management API is not checked: it
is a third listener that can be up while both doors are shut.

A door that fails `failures_to_raise` probes running raises one
`gw.alert` (PlatformService, BrokerUnreachable, Subject the door as
host:port) into the store; the first success resolves it. The tap pages
the record on its reconcile pass from the store, with no broker
involved. A network fault at the alerts box looks the same as a broker
fault from there, and the summary says so.
"""

from __future__ import annotations

import logging
import socket
import ssl
import struct
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

import pika
from pydantic import TypeAdapter

from gwalerter.config import AlerterSettings
from gwalerter.sema.enums import AlertCategory, AlertState, PlatformAlertKind
from gwalerter.sema.property_format import LeftRightDot, UTCMilliseconds
from gwalerter.sema.types import Alert
from gwalerter.store import Store

CONNECT_TIMEOUT_S = 10.0
UTC_MS = TypeAdapter(UTCMilliseconds)
# MQTT 3.1.1 CONNECT with a fixed client id and no credentials; the
# broker answers CONNACK (accepted or refused) if its MQTT door is open.
MQTT_CLIENT_ID = b"gwalerter-probe"
MQTT_CONNACK = 0x20


class Door(NamedTuple):
    """One door of the broker the prober checks: `name` is the Subject of
    the alert it raises (host:port), `check` raises on failure and returns
    on success."""

    name: str
    check: Callable[[], None]


class Outcome(NamedTuple):
    """One probe of one door: `ok`, and the failure's text when not."""

    ok: bool
    reason: str


def iso_utc(ms: UTCMilliseconds) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def amqp_round_trip(url: str) -> None:
    """Connect, declare an exclusive auto-delete queue, publish one message
    to it through the default exchange and receive it back."""
    params = pika.URLParameters(url)
    params.socket_timeout = CONNECT_TIMEOUT_S
    params.blocked_connection_timeout = CONNECT_TIMEOUT_S
    connection = pika.BlockingConnection(params)
    try:
        channel = connection.channel()
        queue = channel.queue_declare("", exclusive=True, auto_delete=True).method.queue
        token = uuid.uuid4().bytes
        channel.basic_publish("", queue, token)
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        while time.monotonic() < deadline:
            method, _properties, body = channel.basic_get(queue, auto_ack=True)
            if method is not None:
                if body != token:
                    raise RuntimeError("round trip returned another message")
                return
            connection.sleep(0.1)
        raise TimeoutError("published message did not come back")
    finally:
        connection.close()


def mqtt_connack(host: str, port: int, *, tls: bool) -> None:
    """TLS handshake (the fleet's listener; the dev broker's is plain),
    MQTT CONNECT, read a CONNACK. Refused credentials are a CONNACK too:
    the door is open."""
    with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S) as raw:
        sock = (
            ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            if tls
            else raw
        )
        with sock:
            payload = struct.pack("!H", len(MQTT_CLIENT_ID)) + MQTT_CLIENT_ID
            # Variable header: protocol name "MQTT", level 4, clean session,
            # keep-alive 10 s.
            variable = struct.pack("!H4sBBH", 4, b"MQTT", 4, 0x02, 10)
            body = variable + payload
            sock.sendall(bytes([0x10, len(body)]) + body)
            head = sock.recv(4)
            if len(head) < 4 or head[0] != MQTT_CONNACK:
                raise RuntimeError(f"no CONNACK (got {head.hex()})")


def doors(settings: AlerterSettings) -> list[Door]:
    return [
        Door(
            name=amqp_door_name(settings.probe_amqp_url.get_secret_value()),
            check=lambda: amqp_round_trip(settings.probe_amqp_url.get_secret_value()),
        ),
        Door(
            name=f"{settings.probe_mqtt_host}:{settings.probe_mqtt_port}",
            check=lambda: mqtt_connack(
                settings.probe_mqtt_host,
                settings.probe_mqtt_port,
                tls=settings.probe_mqtt_tls,
            ),
        ),
    ]


def amqp_door_name(url: str) -> str:
    """The AMQP door as host:port, the Subject of its alert; never the URL,
    which carries the credential."""
    params = pika.URLParameters(url)
    return f"{params.host}:{params.port}"


class Prober:
    def __init__(
        self,
        *,
        settings: AlerterSettings,
        store: Store,
        doors: list[Door],
        clock_ms: Callable[[], UTCMilliseconds] = lambda: UTC_MS.validate_python(
            int(time.time() * 1000)
        ),
    ) -> None:
        self.settings = settings
        self.store = store
        self.doors = doors
        self.clock_ms = clock_ms
        self.src: LeftRightDot = settings.service_alias
        self.failures_to_raise = settings.probe_failures_to_raise
        self.failures: dict[str, int] = {door.name: 0 for door in doors}
        self.stop_event = threading.Event()
        self.logger = logging.getLogger(__name__)

    def probe(self, door: Door) -> Outcome:
        try:
            door.check()
            return Outcome(ok=True, reason="")
        except Exception as e:  # noqa: BLE001 -- any failure is the finding
            return Outcome(ok=False, reason=repr(e))

    def tick(self) -> list[Alert]:
        """Probe every door once; raise on the door that has failed
        `failures_to_raise` probes running and has no alert open, resolve on
        the first success of a door with one open. Records returned are
        already in the store."""
        now_ms = self.clock_ms()
        records: list[Alert] = []
        for door in self.doors:
            outcome = self.probe(door)
            open_alert = self.open_alert(door.name)
            if outcome.ok:
                self.failures[door.name] = 0
                if open_alert is not None:
                    records.append(self.resolve(door, open_alert, now_ms))
                continue
            self.failures[door.name] += 1
            self.logger.warning(
                "%s failed (%d running): %s",
                door.name,
                self.failures[door.name],
                outcome.reason,
            )
            if (
                open_alert is None
                and self.failures[door.name] >= self.failures_to_raise
            ):
                records.append(self.raise_alert(door, outcome, now_ms))
        return records

    def raise_alert(
        self, door: Door, outcome: Outcome, now_ms: UTCMilliseconds
    ) -> Alert:
        alert = Alert(
            src=self.src,
            category=AlertCategory.PlatformService,
            kind=PlatformAlertKind.BrokerUnreachable,
            state=AlertState.Firing,
            subject=door.name,
            alert_id=str(uuid.uuid4()),
            raised_ms=now_ms,
            summary=(
                f"Broker door {door.name} failed {self.failures[door.name]} probes "
                f"running from the alerts box; last: {outcome.reason}"
            ),
            evidence=[],
        )
        self.store.raise_alert(alert)
        self.logger.error(
            "Firing BrokerUnreachable on %s (%s)", door.name, alert.alert_id
        )
        return alert

    def resolve(self, door: Door, open_alert: Alert, now_ms: UTCMilliseconds) -> Alert:
        resolved = Alert(
            src=self.src,
            category=AlertCategory.PlatformService,
            kind=PlatformAlertKind.BrokerUnreachable,
            state=AlertState.Resolved,
            subject=door.name,
            alert_id=open_alert.alert_id,
            raised_ms=open_alert.raised_ms,
            resolved_ms=now_ms,
            summary=f"Broker door {door.name} answered again at {iso_utc(now_ms)}",
            evidence=[],
        )
        self.store.clear_alert(resolved)
        self.logger.info(
            "Resolved BrokerUnreachable on %s (%s)", door.name, resolved.alert_id
        )
        return resolved

    def open_alert(self, door_name: str) -> Alert | None:
        return self.store.open_alert(
            AlertCategory.PlatformService,
            PlatformAlertKind.BrokerUnreachable,
            subject=door_name,
        )

    def run(self) -> None:
        """Probe on the interval until stopped."""
        self.logger.info(
            "Probing %s every %d s; raising after %d failures",
            [door.name for door in self.doors],
            self.settings.probe_interval_s,
            self.failures_to_raise,
        )
        try:
            while not self.stop_event.is_set():
                self.tick()
                self.stop_event.wait(self.settings.probe_interval_s)
        except KeyboardInterrupt:
            pass
        finally:
            self.store.close()

    def stop(self) -> None:
        self.stop_event.set()
