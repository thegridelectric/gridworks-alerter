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

The told set starts empty at boot, so the first pass re-creates every
open alert; Opsgenie folds those into the alerts it already has, so a
tap restart neither re-pages nor forgets. A post that fails after its
retries is logged and left for the next pass: the store and the journal
are the durable record.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import httpx

from gwalerter.config import AlerterSettings
from gwalerter.sema.enums import OpsgeniePriority
from gwalerter.sema.types import Alert, OpsgenieAlertClose, OpsgenieAlertCreate
from gwalerter.store import Store

ALERTS_PATH = "/v2/alerts"
POST_ATTEMPTS = 3
POST_RETRY_S = 2.0
# Opsgenie's limit on the headline `message`.
MESSAGE_MAX_CHARS = 130
# Every alert the alerter raises pages: a starting value, tuned per kind
# once the shadow week shows which kinds a person must act on at once.
PRIORITY = OpsgeniePriority.P1


def house_short_name(about_g_node_alias: str) -> str:
    """The headline name: the segment before `.ta` (`…keene.spruce.ta` →
    `spruce`). Display only; the full alias stays the identity."""
    return about_g_node_alias.removesuffix(".ta").rsplit(".", 1)[-1]


def to_opsgenie(
    word: Alert, *, display_name: str | None, team_id: str
) -> OpsgenieAlertCreate:
    """Map one `Firing` record onto the create word. The headline is
    `[house] summary` for a house alert, else the summary; `Entity` is
    the full alias (or `Subject`, or `Src`), so two houses with one short
    name are two alerts; the named details carry the facts the headline
    cannot."""
    subject = word.about_g_node_alias or word.subject or word.src
    message = word.summary
    house = None
    about = None
    if word.about_g_node_alias is not None:
        house = house_short_name(word.about_g_node_alias)
        message = f"[{house}] {word.summary}"
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


def to_opsgenie_close(word: Alert) -> OpsgenieAlertClose:
    """Map one `Resolved` record onto the close of its alias, with the
    summary as the note a reader sees on the closed alert."""
    return OpsgenieAlertClose(alias=word.alert_id, source=word.src, note=word.summary)


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
    """Opsgenie's close request from the word; the alias rides the URL."""
    return {"source": close.source, "note": close.note}


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.store = store
        self.opsgenie = opsgenie or OpsgenieClient(
            settings.opsgenie_url,
            api_key=settings.opsgenie_api_key.get_secret_value(),
        )
        self.clock = clock
        self.reconcile_s = settings.tap_reconcile_s
        # The alerts Opsgenie has been told are open, by AlertId.
        self.told: dict[str, Alert] = {}
        self.last_reconcile = clock()
        self.stop_event = threading.Event()
        self.logger = logging.getLogger(__name__)

    # -- the told set ---------------------------------------------------------

    def create(self, word: Alert) -> None:
        if self.opsgenie.create(self.mapped(word)):
            self.told[word.alert_id] = word

    def close(self, word: Alert) -> None:
        if self.opsgenie.close(to_opsgenie_close(word)):
            self.told.pop(word.alert_id, None)

    def reconcile(self) -> None:
        """The store is the truth about which alerts are open: tell
        Opsgenie of any open alert it has not taken, and close any it
        holds that the store has resolved (a `Resolved` record missed on
        the broker, or a close that failed)."""
        store_open = {word.alert_id: word for word in self.store.open_alerts()}
        for alert_id, word in store_open.items():
            if alert_id not in self.told:
                self.logger.info("Reconcile: creating %s", alert_id)
                self.create(word)
        for alert_id, word in list(self.told.items()):
            if alert_id not in store_open:
                self.logger.info("Reconcile: closing %s", alert_id)
                self.close(self.store.resolved_record(alert_id) or word)
        self.last_reconcile = self.clock()

    def reconcile_due(self) -> bool:
        return self.clock() - self.last_reconcile >= self.reconcile_s

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
