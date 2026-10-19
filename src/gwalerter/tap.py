"""The tap: pages the alerter's open alerts through Opsgenie and closes
them when they resolve.

It is its own process with no broker connection, so the alert that says
the broker is down still reaches a person. It polls the store and
reconciles Opsgenie against it, keying each Opsgenie alert on the
AlertId, so an alert told twice is one alert and a re-raise after a
resolve is a new one. Who is paged, how often an open alert re-notifies
and how it escalates are Opsgenie's policy.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field

from gwalerter.config import AlerterSettings
from gwalerter.duration import minutes
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
    """The last segment of the house alias before the terminal-asset
    suffix, for a headline."""
    return about_g_node_alias.removesuffix(".ta").rsplit(".", 1)[-1]


def to_opsgenie(
    word: Alert, *, display_name: str | None, team_id: str
) -> OpsgenieAlertCreate:
    """The create word for one `Firing` record, headline and details
    included."""
    subject = word.about_g_node_alias or word.subject or word.src
    message = word.summary
    house = None
    about = None
    if word.about_g_node_alias is not None:
        house = house_short_name(word.about_g_node_alias)
        message = f"[{house}] [{word.kind.value}] {word.summary}"
        about = word.about_g_node_alias
        if display_name:
            about = f"{about} ({display_name})"
    return OpsgenieAlertCreate(
        alias=word.alert_id,
        message=message[:MESSAGE_MAX_CHARS],
        description=word.summary,
        entity=subject,  # the full alias: two houses with one short name are two alerts
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
    """The close word for the record's alias; the note is the given one,
    else the record's summary."""
    return OpsgenieAlertClose(
        alias=word.alert_id, source=word.src, note=note or word.summary
    )


def silenced_note(word: Alert, now_ms: int) -> str:
    return (
        f"Silenced after {minutes(now_ms - word.raised_ms)} ({word.about_g_node_alias})"
    )


def create_body(create: OpsgenieAlertCreate) -> dict[str, object]:
    """Opsgenie's create request body for the word."""
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
    """Opsgenie's ISO 8601 instant as UTC milliseconds."""
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)


class ListedReport(BaseModel):
    """The `report` block of an alert as Opsgenie lists it: who acted on
    it."""

    model_config = ConfigDict(extra="ignore")

    acknowledged_by: str | None = Field(default=None, alias="acknowledgedBy")
    closed_by: str | None = Field(default=None, alias="closedBy")


class ListedItem(BaseModel):
    """One alert as Opsgenie's list endpoint gives it, the fields read."""

    model_config = ConfigDict(extra="ignore")

    alias: str
    id: str
    tiny_id: str | None = Field(default=None, alias="tinyId")
    message: str
    status: str
    acknowledged: bool
    count: int
    priority: str
    source: str
    tags: list[str]
    created_at: str = Field(alias="createdAt")
    last_occurred_at: str = Field(alias="lastOccurredAt")
    report: ListedReport | None = None


def listed_alert(item: dict[str, object]) -> OpsgenieAlert:
    """One alert as Opsgenie's list endpoint gives it, read as the word."""
    listed = ListedItem.model_validate(item)
    report = listed.report or ListedReport()
    return OpsgenieAlert(
        alias=listed.alias,
        opsgenie_id=listed.id,
        tiny_id=listed.tiny_id or None,
        message=listed.message,
        status=(
            OpsgenieAlertStatus.Closed
            if listed.status == "closed"
            else OpsgenieAlertStatus.Open
        ),
        acknowledged=listed.acknowledged,
        count=listed.count,
        priority=OpsgeniePriority(listed.priority),
        source=listed.source,
        tags=listed.tags,
        created_ms=opsgenie_ms(listed.created_at),
        last_occurred_ms=opsgenie_ms(listed.last_occurred_at),
        acknowledged_by=report.acknowledged_by or None,
        closed_by=report.closed_by or None,
    )


class OpsgenieClient:
    """Creates and closes alerts through Opsgenie's Alert API. A transport
    failure is retried a few times before the call gives up. Opsgenie
    accepts a request and processes it later, so a True result means the
    request was accepted and the alert may still be refused."""

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
        """One pass: Opsgenie's open alerts are made to match the store's
        open alerts about unsilenced houses, with Opsgenie's listing as
        what it holds."""
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
