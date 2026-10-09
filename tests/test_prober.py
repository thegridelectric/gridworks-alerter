"""The prober: a door that fails three probes running raises one
BrokerUnreachable into the store, the first success resolves it, the tap
pages a store-written alert on its reconcile pass, and the two real
checks answer against the dev broker."""

from __future__ import annotations

import ssl
import subprocess
import time
from pathlib import Path

import pika
import pytest
from gwbase.config.rabbit_settings import RabbitTls

from gwalerter.config import AlerterSettings
from gwalerter.prober import (
    Door,
    Prober,
    amqp_door_name,
    amqp_parameters,
    amqp_round_trip,
    mqtt_connack,
    mqtt_ssl_context,
    probe_claims,
)
from gwalerter.sema.enums import AlertCategory, AlertState, PlatformAlertKind
from gwalerter.store import Store
from gwalerter.tap import Tap
from tests.conftest import broker_up
from tests.test_tap import RecordingOpsgenie

T0 = 1_800_000_000_000
AMQP = "hw1-1.electricity.works:5671"
MQTT = "hw1-1.electricity.works:8883"


class Flaky:
    """A door whose next outcomes are scripted: `fail` failures, then
    success."""

    def __init__(self, fail: int = 0) -> None:
        self.fail = fail
        self.calls = 0

    def check(self) -> None:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionRefusedError("connection refused")


def prober(settings: AlerterSettings, store: Store, amqp: Flaky, mqtt: Flaky) -> Prober:
    now = [T0]
    p = Prober(
        settings=settings,
        store=store,
        doors=[Door(AMQP, amqp.check), Door(MQTT, mqtt.check)],
        clock_ms=lambda: now[0],
    )
    p.now = now  # type: ignore[attr-defined]
    return p


def open_broker_alerts(store: Store) -> list[str]:
    return [
        a.subject or ""
        for a in store.open_alerts()
        if a.kind is PlatformAlertKind.BrokerUnreachable
    ]


def test_three_failures_raise_once_and_a_success_resolves(
    settings: AlerterSettings, store: Store
) -> None:
    amqp, mqtt = Flaky(fail=4), Flaky()
    p = prober(settings, store, amqp, mqtt)
    assert p.tick() == [] and p.tick() == []
    assert open_broker_alerts(store) == []
    (firing,) = p.tick()
    assert firing.state is AlertState.Firing
    assert firing.category is AlertCategory.PlatformService
    assert firing.kind is PlatformAlertKind.BrokerUnreachable
    assert firing.subject == AMQP and firing.about_g_node_alias is None
    assert "3 probes" in firing.summary and "alerts box" in firing.summary
    assert open_broker_alerts(store) == [AMQP]
    assert p.tick() == []  # a fourth failure: still the one alert
    assert open_broker_alerts(store) == [AMQP]
    (resolved,) = p.tick()  # the fifth probe succeeds
    assert resolved.state is AlertState.Resolved
    assert resolved.alert_id == firing.alert_id
    assert resolved.raised_ms == firing.raised_ms
    assert open_broker_alerts(store) == []
    assert mqtt.calls == 5


def test_each_door_is_its_own_alert(settings: AlerterSettings, store: Store) -> None:
    p = prober(settings, store, Flaky(fail=3), Flaky(fail=3))
    p.tick()
    p.tick()
    records = p.tick()
    assert sorted(r.subject or "" for r in records) == sorted([AMQP, MQTT])
    assert sorted(open_broker_alerts(store)) == sorted([AMQP, MQTT])


def test_a_restart_neither_re_raises_nor_forgets(
    settings: AlerterSettings, store: Store
) -> None:
    p = prober(settings, store, Flaky(fail=3), Flaky())
    p.tick()
    p.tick()
    p.tick()
    assert open_broker_alerts(store) == [AMQP]
    # A fresh prober on the same store: the door still fails, the alert
    # stays the one that is open; when the door answers, it resolves it.
    p2 = prober(settings, store, Flaky(fail=1), Flaky())
    assert p2.tick() == []
    assert open_broker_alerts(store) == [AMQP]
    (resolved,) = p2.tick()
    assert resolved.state is AlertState.Resolved
    assert open_broker_alerts(store) == []


def test_the_tap_pages_a_store_written_alert(
    settings: AlerterSettings, store: Store
) -> None:
    """The prober has no broker; the tap's reconcile pass pages what the
    store holds open, and closes it when the store has resolved it."""
    p = prober(settings, store, Flaky(fail=3), Flaky())
    p.tick()
    p.tick()
    (firing,) = p.tick()
    og = RecordingOpsgenie()
    tap = Tap(
        settings=settings, store=store, opsgenie=og.client(), clock=time.monotonic
    )
    tap.reconcile()
    assert og.verbs() == [("create", firing.alert_id)]
    create = og.requests[0].body
    assert create["entity"] == AMQP and create["details"]["kind"] == "BrokerUnreachable"
    assert "house" not in create["details"]
    p.tick()  # the door answers: resolved in the store
    tap.reconcile()
    assert og.verbs()[-1] == ("close", firing.alert_id)


def test_amqp_door_name_drops_the_credential() -> None:
    assert (
        amqp_door_name("amqps://user:secret@hw1-1.electricity.works:5671/hw1__1")
        == AMQP
    )


# -- against the dev broker ------------------------------------------------


@pytest.mark.broker
@pytest.mark.skipif(not broker_up(), reason="gwbase dev broker not on localhost:5672")
def test_both_doors_answer_on_the_dev_broker(settings: AlerterSettings) -> None:
    # The actor's broker client carries the dev broker's real credentials;
    # the probe block in conftest is a placeholder.
    amqp_round_trip(settings.rabbit, claims=None)
    mqtt_connack("localhost", 1885, context=None)


def test_probe_claims_are_the_probers_own(settings: AlerterSettings) -> None:
    """The prober connects as `<alias>.probe` on the probe URL's run with the
    instance id it was given: its own principal, never the alerter's."""
    claims = probe_claims(settings, "6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
    assert claims.alias == f"{settings.service_alias}.probe"
    assert claims.run == settings.probe_amqp.run == "d1__1"
    assert claims.instance_id == "6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_password_probe_has_no_claims_and_a_tls_probe_needs_them(
    settings: AlerterSettings,
) -> None:
    """Without a tls block the URL's credentials stand; a tls block with
    no claims is a programming error, not a silent password fallback."""
    params = amqp_parameters(settings.probe_amqp, claims=None)
    assert params.ssl_options is None
    assert isinstance(params.credentials, pika.PlainCredentials)
    assert params.credentials.username == "u"
    tls = settings.probe_amqp.model_copy(
        update={
            "url": settings.probe_amqp.url,
            "tls": {"ca_cert_path": "/x", "cert_path": "/y", "private_key_path": "/z"},
        }
    )
    with pytest.raises(ValueError, match="claims"):
        amqp_parameters(tls, claims=None)


def test_a_shut_door_fails() -> None:
    with pytest.raises(OSError):
        mqtt_connack("localhost", 1, context=None)


def test_mqtt_door_trusts_the_probe_ca(tmp_path: Path) -> None:
    """The fleet broker's cert chains to the GridWorks CA, which no system
    store holds: a tls block makes that CA the door's trust root and
    presents the probe's cert. Field finding 2026-10-08: the door verified
    against the system store and counted the broker unreachable."""
    ca = tmp_path / "ca.crt"
    key = tmp_path / "ca.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=throwaway-ca",
            "-keyout",
            str(key),
            "-out",
            str(ca),
        ],
        check=True,
        capture_output=True,
    )
    ctx = mqtt_ssl_context(
        RabbitTls(ca_cert_path=ca, cert_path=ca, private_key_path=key)
    )
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.cert_store_stats()["x509_ca"] == 1
    assert ctx.get_ca_certs()[0]["subject"] == ((("commonName", "throwaway-ca"),),)
