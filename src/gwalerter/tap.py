"""The tap: every alert open in the alerter's store becomes an Opsgenie
alert, and its `Resolved` record closes it.

A poller of the store beside the actor, with no broker connection: the
one alert that means the broker is down has to reach a person while the
broker is down. Every `tap_reconcile_s` it reads the store's open alerts
and reconciles Opsgenie against them: an open alert Opsgenie has not
been told of is created, one Opsgenie holds that the store has resolved
is closed. Each record is mapped onto the tap's outbound words,
`gw.opsgenie.alert.create` and `gw.opsgenie.alert.close`, which the post
writes into Opsgenie's request shape: the alias is the `AlertId`, which
is what Opsgenie deduplicates on, so an alert told twice is one alert
and a re-raise after a resolve is a new one. Which people are paged, how
often an open alert re-notifies and how it escalates are Opsgenie's
policy, not the tap's.

Each pass starts by listing what Opsgenie holds open from this alerter,
each alert read as `gw.opsgenie.alert`; that listing is the told set, so
a tap restart, a failed close and a close made by hand are all seen on
the next pass. When the listing cannot be read the last told set stands
(empty at boot, so the first pass re-creates every open alert and
Opsgenie folds those into the alerts it already has). A post that fails
after its retries is logged and left for the next pass: the store and
the journal are the durable record.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

import httpx

from gwalerter.config import AlerterSettings
from gwalerter.duration import minutes
from gwalerter.sema.codec import default_codec
from gwalerter.sema.enums import OpsgenieAlertStatus, OpsgeniePriority
from gwalerter.sema.types import (
    Alert,
    OpsgenieAlert,
    OpsgenieAlertClose,
    OpsgenieAlertCreate,
)
from gwalerter.store import Store

ALERTS_PATH = "/v2/alerts"
# Opsgenie's largest page when listing alerts.
LIST_PAGE = 100
POST_ATTEMPTS = 3
POST_RETRY_S = 2.0
# Opsgenie's limit on the headline `message`.
MESSAGE_MAX_CHARS = 130
# Every alert the alerter raises pages: a starting value, tuned per kind
# once the shadow week shows which kinds a person must act on at once.
PRIORITY = OpsgeniePriority.P1
# The close note for an alias Opsgenie holds open from this alerter that
# the store neither holds open nor has resolved.
NOT_IN_STORE_NOTE = "Closed by the alerter: not open in its store"


def house_short_name(about_g_node_alias: str) -> str:
    """The headline name: the segment before `.ta` (`…keene.spruce.ta` →
    `spruce`). Display only; the full alias stays the identity."""
    return about_g_node_alias.removesuffix(".ta").rsplit(".", 1)[-1]


def to_opsgenie(
    word: Alert, *, display_name: str | None, team_id: str
) -> OpsgenieAlertCreate:
    """Map one `Firing` record onto the create word. The headline is
    `[House] summary` for a house alert, else the summary; `Entity` is
    the full alias (or `Subject`, or `Src`), so two houses with one short
    name are two alerts; the named details carry the facts the headline
    cannot."""
    subject = word.about_g_node_alias or word.subject or word.src
    message = word.summary
    house = None
    about = None
    if word.about_g_node_alias is not None:
        house = house_short_name(word.about_g_node_alias)
        message = f"[{house.capitalize()}] {word.summary}"
        about = word.about_g_node_alias
        if display_name:
            about = f"{about} ({display_name})"
    return OpsgenieAlertCreate(
        alias=word.alert_id,
        message=message[:MESSAGE_MAX_CHARS],
        description=word.summary,
        entity=subject,
        source=word.src,
        priority=PRIORITY,
        responder_team_id=team_id,
        tags=[word.category.value, word.kind.value],
        kind=word.kind.value,
        category=word.category.value,
        subject=subject,
        house=house,
        about=about,
    )


def to_opsgenie_close(word: Alert, note: str | None = None) -> OpsgenieAlertClose:
    """Map one record onto the close of its alias, with the summary (a
    `Resolved` record's) or the given note as what a reader sees on the
    closed alert."""
    return OpsgenieAlertClose(
        alias=word.alert_id, source=word.src, note=note or word.summary
    )


def silenced_note(word: Alert, now_ms: int) -> str:
    return (
        f"Silenced after {minutes(now_ms - word.raised_ms)} ({word.about_g_node_alias})"
    )


def create_body(create: OpsgenieAlertCreate) -> dict[str, object]:
    """Opsgenie's create request from the word: its fields in the API's
    shape, the named details written into Opsgenie's flat string map
    under their lower-case names, the team as the one responder."""
    details = {
        "kind": create.kind,
        "category": create.category,
        "subject": create.subject,
    }
    if create.house is not None:
        details["house"] = create.house
    if create.about is not None:
        details["about"] = create.about
    return {
        "alias": create.alias,
        "message": create.message,
        "description": create.description,
        "entity": create.entity,
        "source": create.source,
        "tags": list(create.tags),
        "details": details,
        "priority": create.priority.value,
        "responders": [{"type": "team", "id": create.responder_team_id}],
    }


def close_body(close: OpsgenieAlertClose) -> dict[str, object]:
    """Opsgenie's close request from the word; the alias is in the URL."""
    return {"source": close.source, "note": close.note}


def opsgenie_ms(stamp: str) -> int:
    """Opsgenie's ISO 8601 instant (`2026-10-15T19:00:11.482Z`) as UTC ms."""
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)


def listed_alert(item: dict[str, object]) -> OpsgenieAlert:
    """One alert as Opsgenie's list endpoint gives it, read as the word: the
    fields GridWorks reads lifted into the word's names, the rest left
    behind."""
    report = item.get("report")
    report = report if isinstance(report, dict) else {}
    status = (
        OpsgenieAlertStatus.Closed
        if item["status"] == "closed"
        else OpsgenieAlertStatus.Open
    )
    wire: dict[str, object] = {
        "TypeName": "gw.opsgenie.alert",
        "Version": "000",
        "Alias": item["alias"],
        "OpsgenieId": item["id"],
        "TinyId": item.get("tinyId") or None,
        "Message": item["message"],
        "Status": status.value,
        "Acknowledged": item["acknowledged"],
        "Count": item["count"],
        "Priority": item["priority"],
        "Source": item["source"],
        "Tags": item["tags"],
        "CreatedMs": opsgenie_ms(str(item["createdAt"])),
        "LastOccurredMs": opsgenie_ms(str(item["lastOccurredAt"])),
        "AcknowledgedBy": report.get("acknowledgedBy") or None,
        "ClosedBy": report.get("closedBy") or None,
    }
    return default_codec.from_dict(
        {k: v for k, v in wire.items() if v is not None}, expect=OpsgenieAlert
    )


class OpsgenieClient:
    """Creates and closes alerts through Opsgenie's Alert API, retrying a
    transport failure a few times and giving up after that. Opsgenie
    accepts a request (202) and processes it asynchronously, so a 202 is
    "taken", not "done"."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        attempts: int = POST_ATTEMPTS,
        retry_s: float = POST_RETRY_S,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = base_url.rstrip("/") + ALERTS_PATH
        self.attempts = attempts
        self.retry_s = retry_s
        self.client = httpx.Client(
            timeout=10.0,
            transport=transport,
            headers={"Authorization": f"GenieKey {api_key}"},
        )
        self.sleep = sleep
        self.logger = logging.getLogger(__name__)

    def create(self, create: OpsgenieAlertCreate) -> bool:
        return self.post(self.url, create_body(create), f"create {create.alias}")

    def close(self, close: OpsgenieAlertClose) -> bool:
        return self.post(
            f"{self.url}/{close.alias}/close?identifierType=alias",
            close_body(close),
            f"close {close.alias}",
        )

    def list_open(self, source: str) -> list[OpsgenieAlert] | None:
        """Every alert Opsgenie holds open from `source`, each read as
        `gw.opsgenie.alert`; None when the listing could not be read (a
        transport error or an error status, logged). An item the word
        refuses is logged and left out."""
        query = f'status:open AND source:"{source}"'
        alerts: list[OpsgenieAlert] = []
        offset = 0
        while True:
            try:
                response = self.client.get(
                    self.url,
                    params={"query": query, "limit": LIST_PAGE, "offset": offset},
                )
                response.raise_for_status()
            except httpx.TransportError as e:
                self.logger.warning("Opsgenie unreachable for the listing: %r", e)
                return None
            except httpx.HTTPStatusError as e:
                self.logger.error(
                    "Opsgenie refused the listing: %s %s",
                    e.response.status_code,
                    e.response.text,
                )
                return None
            items = response.json().get("data") or []
            for item in items:
                try:
                    alerts.append(listed_alert(item))
                except Exception as e:  # noqa: BLE001 -- one bad item never hides the rest
                    self.logger.error(
                        "Dropped a listed alert %r: %r", item.get("alias"), e
                    )
            if len(items) < LIST_PAGE:
                return alerts
            offset += LIST_PAGE

    def post(self, url: str, body: dict[str, object], what: str) -> bool:
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.client.post(url, json=body)
                response.raise_for_status()
                return True
            except httpx.TransportError as e:
                self.logger.warning(
                    "Opsgenie unreachable for %s (attempt %d/%d): %r",
                    what,
                    attempt,
                    self.attempts,
                    e,
                )
                if attempt < self.attempts:
                    self.sleep(self.retry_s)
            except httpx.HTTPStatusError as e:
                self.logger.error(
                    "Opsgenie refused %s: %s %s",
                    what,
                    e.response.status_code,
                    e.response.text,
                )
                return False
        self.logger.error("Gave up on %s after %d attempts", what, self.attempts)
        return False

    def shutdown(self) -> None:
        self.client.close()


class Tap:
    def __init__(
        self,
        *,
        settings: AlerterSettings,
        store: Store,
        opsgenie: OpsgenieClient | None = None,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.settings = settings
        self.store = store
        self.opsgenie = opsgenie or OpsgenieClient(
            settings.opsgenie_url,
            api_key=settings.opsgenie_api_key.get_secret_value(),
        )
        self.wall_ms = wall_ms
        self.reconcile_s = settings.tap_reconcile_s
        self.src = settings.service_alias
        # The aliases Opsgenie holds open from this alerter: the listing
        # when it can be read, else the last one kept by creates and closes.
        self.told: set[str] = set()
        self.stop_event = threading.Event()
        self.logger = logging.getLogger(__name__)

    # -- the told set ---------------------------------------------------------

    def create(self, word: Alert) -> None:
        if self.opsgenie.create(self.mapped(word)):
            self.told.add(word.alert_id)

    def close(self, close: OpsgenieAlertClose) -> None:
        if self.opsgenie.close(close):
            self.told.discard(close.alias)

    def reconcile(self) -> None:
        """The store is the truth about which alerts are open and which
        houses are silenced, and Opsgenie's listing is the truth about
        what it has been told: tell Opsgenie of any open alert about an
        unsilenced house it does not hold, close any it holds about a
        house since silenced, and close any it holds that the store does
        not hold open (resolved, or never this store's)."""
        listed = self.opsgenie.list_open(self.src)
        if listed is not None:
            self.told = {alert.alias for alert in listed}
        silenced = set(self.store.silenced_aliases())
        store_open = {word.alert_id: word for word in self.store.open_alerts()}
        paged = {
            alert_id: word
            for alert_id, word in store_open.items()
            if word.about_g_node_alias not in silenced
        }
        for alert_id, word in paged.items():
            if alert_id not in self.told:
                self.logger.info("Reconcile: creating %s", alert_id)
                self.create(word)
        for alert_id in sorted(self.told):
            if alert_id in paged:
                continue
            if alert_id in store_open:
                self.logger.info("Reconcile: closing silenced %s", alert_id)
                word = store_open[alert_id]
                self.close(to_opsgenie_close(word, silenced_note(word, self.wall_ms())))
                continue
            self.logger.info("Reconcile: closing %s", alert_id)
            resolved = self.store.resolved_record(alert_id)
            if resolved is not None:
                self.close(to_opsgenie_close(resolved))
            else:
                self.close(
                    OpsgenieAlertClose(
                        alias=alert_id, source=self.src, note=NOT_IN_STORE_NOTE
                    )
                )

    def mapped(self, word: Alert) -> OpsgenieAlertCreate:
        display_name = None
        if word.about_g_node_alias is not None:
            node = self.store.g_node(word.about_g_node_alias)
            display_name = node.display_name if node is not None else None
        return to_opsgenie(
            word, display_name=display_name, team_id=self.settings.opsgenie_team_id
        )

    # -- the loop -------------------------------------------------------------

    def run(self) -> None:
        """Reconcile on the cadence until stopped."""
        self.logger.info(
            "Paging the store's open alerts to %s every %d s",
            self.opsgenie.url,
            self.reconcile_s,
        )
        try:
            while not self.stop_event.is_set():
                self.reconcile()
                self.stop_event.wait(self.reconcile_s)
        except KeyboardInterrupt:
            pass
        finally:
            self.opsgenie.shutdown()
            self.store.close()

    def stop(self) -> None:
        self.stop_event.set()
