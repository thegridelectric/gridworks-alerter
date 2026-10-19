"""The tap: an alert open in the store creates an Opsgenie alert aliased
by its `AlertId`, its `Resolved` record closes that alias, each pass
reads what Opsgenie holds open as `gw.opsgenie.alert` and reconciles
against it (a tap restart, a failed post, a close made by hand), a
refused post is retried then left for the next pass, and none of it
needs a broker. Opsgenie itself is a recording httpx transport here that
lists what it has been told."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import NamedTuple

import httpx
import pytest

from gwalerter.config import AlerterSettings
from gwalerter.duration import minutes
from gwalerter.sema.enums import (
    AlertCategory,
    AlertState,
    HouseAlertKind,
    OpsgenieAlertStatus,
    OpsgeniePriority,
    PlatformAlertKind,
)
from gwalerter.sema.types import Alert, HackAlertSilences
from gwalerter.store import Store
from gwalerter.tap import (
    MESSAGE_MAX_CHARS,
    NOT_IN_STORE_NOTE,
    OpsgenieClient,
    Tap,
    close_body,
    create_body,
    house_short_name,
    listed_alert,
    to_opsgenie,
    to_opsgenie_close,
)
from tests.conftest import FLEET_ROOT

HOUSE = f"{FLEET_ROOT}.spruce"
T0 = 1_800_000_000_000
TEAM = "team-0000"


def firing(alert_id: str | None = None) -> Alert:
    return Alert(
        src="d1.alerts",
        category=AlertCategory.House,
        kind=HouseAlertKind.NoData,
        state=AlertState.Firing,
        about_g_node_alias=f"{HOUSE}.ta",
        alert_id=alert_id or str(uuid.uuid4()),
        raised_ms=T0,
        summary="No data from spruce",
        evidence=[],
    )


def resolved(of: Alert) -> Alert:
    return Alert(
        src=of.src,
        category=of.category,
        kind=of.kind,
        state=AlertState.Resolved,
        about_g_node_alias=of.about_g_node_alias,
        alert_id=of.alert_id,
        raised_ms=of.raised_ms,
        resolved_ms=T0 + 60_000,
        summary="Data from spruce again",
        evidence=[],
    )


class Request(NamedTuple):
    """One request Opsgenie took: `verb` is `create` or `close`, `alias`
    the alert it names, `body` what was posted."""

    verb: str
    alias: str
    body: dict


def listing_item(
    alias: str, body: dict, created_at: str = "2026-10-15T19:00:11.482Z"
) -> dict:
    """One alert as Opsgenie's list endpoint gives it, from the create
    body it was made with."""
    return {
        "id": f"id-{alias[:8]}",
        "tinyId": "41",
        "alias": alias,
        "message": body["message"],
        "status": "open",
        "acknowledged": False,
        "isSeen": False,
        "tags": body["tags"],
        "snoozed": False,
        "count": 1,
        "lastOccurredAt": created_at,
        "createdAt": created_at,
        "updatedAt": created_at,
        "source": body["source"],
        "owner": "",
        "priority": body["priority"],
        "responders": body["responders"],
        "report": {},
    }


class RecordingOpsgenie:
    """An httpx transport standing in for Opsgenie's Alert API: records
    every create and close, holds the set of aliases open and lists it,
    checks the key, refuses the first `refuse` connections, and refuses
    every listing while `listing_fails`."""

    def __init__(self, refuse: int = 0) -> None:
        self.requests: list[Request] = []
        self.open: dict[str, dict] = {}
        self.refuse = refuse
        self.listing_fails = False

    def holds(self, word: Alert) -> None:
        """Opsgenie already has this alert open, from an earlier tap."""
        body = create_body(to_opsgenie(word, display_name=None, team_id=TEAM))
        self.open[word.alert_id] = body

    def transport(self) -> httpx.BaseTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if self.refuse > 0:
                self.refuse -= 1
                raise httpx.ConnectError("connection refused", request=request)
            assert request.headers["Authorization"] == "GenieKey key-0000"
            if request.method == "GET":
                if self.listing_fails:
                    return httpx.Response(503, json={"message": "down"})
                assert request.url.path == "/v2/alerts"
                assert (
                    request.url.params["query"] == 'status:open AND source:"d1.alerts"'
                )
                data = [listing_item(alias, body) for alias, body in self.open.items()]
                return httpx.Response(200, json={"data": data, "paging": {}})
            body = json.loads(request.content)
            if request.url.path == "/v2/alerts":
                self.requests.append(Request("create", body["alias"], body))
                self.open[body["alias"]] = body
            else:
                alias = request.url.path.removeprefix("/v2/alerts/").removesuffix(
                    "/close"
                )
                assert request.url.params["identifierType"] == "alias"
                self.requests.append(Request("close", alias, body))
                self.open.pop(alias, None)
            return httpx.Response(202, json={"result": "Request will be processed"})

        return httpx.MockTransport(handle)

    def client(self) -> OpsgenieClient:
        return OpsgenieClient(
            "https://opsgenie.test",
            api_key="key-0000",
            transport=self.transport(),
            sleep=lambda _s: None,
        )

    def verbs(self) -> list[tuple[str, str]]:
        return [(r.verb, r.alias) for r in self.requests]


def test_firing_maps_to_a_create_word_aliased_by_its_id() -> None:
    word = firing()
    create = to_opsgenie(word, display_name="Spruce (Millinocket)", team_id=TEAM)
    assert create.alias == word.alert_id
    assert create.message == "[spruce] [NoData] No data from spruce"
    assert create.entity == f"{HOUSE}.ta"
    assert create.source == "d1.alerts"
    assert create.tags == ["House", "NoData"]
    assert create.house == "spruce"
    assert create.about == f"{HOUSE}.ta (Spruce (Millinocket))"
    body = create_body(create)
    assert body["details"] == {
        "kind": "NoData",
        "category": "House",
        "subject": f"{HOUSE}.ta",
        "house": "spruce",
        "about": f"{HOUSE}.ta (Spruce (Millinocket))",
    }
    assert body["priority"] == "P1"
    assert body["responders"] == [{"type": "team", "id": TEAM}]


def test_a_service_alert_carries_no_house_details() -> None:
    word = Alert(
        src="d1.alerts",
        category=AlertCategory.PlatformService,
        kind=PlatformAlertKind.Unknown,
        state=AlertState.Firing,
        subject="hw1.alerts",
        alert_id=str(uuid.uuid4()),
        raised_ms=T0,
        summary="A service alert",
        evidence=[],
    )
    create = to_opsgenie(word, display_name=None, team_id=TEAM)
    assert create.house is None and create.about is None
    assert create.entity == "hw1.alerts"
    assert create.message == "A service alert"
    details = create_body(create)["details"]
    assert isinstance(details, dict) and set(details) == {"kind", "category", "subject"}


def test_headline_is_cut_to_opsgenie_limit() -> None:
    word = firing().model_copy(update={"summary": "x" * 200})
    create = to_opsgenie(word, display_name=None, team_id=TEAM)
    assert len(create.message) == MESSAGE_MAX_CHARS


def test_resolved_maps_to_a_close_of_the_alias() -> None:
    word = resolved(firing())
    close = to_opsgenie_close(word)
    assert close.alias == word.alert_id
    assert close_body(close) == {
        "source": "d1.alerts",
        "note": "Data from spruce again",
    }


def test_short_name_is_the_segment_before_ta() -> None:
    assert house_short_name("hw1.isone.me.versant.keene.oak.ta") == "oak"
    assert house_short_name("w.isone.vt.gmp.burlington.oak.ta") == "oak"


def tap(
    settings: AlerterSettings,
    store: Store,
    og: RecordingOpsgenie,
    wall_ms=lambda: T0 + 60_000,
) -> Tap:
    return Tap(
        settings=settings,
        store=store,
        opsgenie=og.client(),
        wall_ms=wall_ms,
    )


def test_firing_creates_and_resolved_closes(
    settings: AlerterSettings, store: Store
) -> None:
    og = RecordingOpsgenie()
    t = tap(settings, store, og)
    word = firing()
    store.raise_alert(word)
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]
    assert word.alert_id in t.told
    store.clear_alert(resolved(word))
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id), ("close", word.alert_id)]
    assert og.requests[-1].body["note"] == "Data from spruce again"
    assert t.told == set()


def test_reconcile_tells_opsgenie_what_the_store_holds_open(
    settings: AlerterSettings, store: Store
) -> None:
    """A tap restart with Opsgenie holding nothing: the store has an open
    alert; the first pass creates it, and a later pass closes it once the
    store has resolved it, with the Resolved summary as the note."""
    word = firing()
    store.raise_alert(word)
    og = RecordingOpsgenie()
    t = tap(settings, store, og)
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]  # already told; nothing new
    # The alerter resolves it but the Resolved record never reaches the
    # tap: the next pass closes it anyway.
    store.clear_alert(resolved(word))
    t.reconcile()
    assert og.verbs()[-1] == ("close", word.alert_id)
    assert og.requests[-1].body["note"] == "Data from spruce again"
    assert t.told == set()


def test_refused_post_is_retried_then_left_for_reconcile(
    settings: AlerterSettings, store: Store
) -> None:
    og = RecordingOpsgenie(refuse=2)
    t = tap(settings, store, og)
    store.raise_alert(word := firing())
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]  # third attempt got through
    # Opsgenie is down for longer than the retries: the create is not
    # told, so the next reconcile pass makes it again.
    store.raise_alert(second := firing())
    og.refuse = 5
    t.reconcile()
    assert second.alert_id not in t.told
    assert len(og.requests) == 1
    t.reconcile()
    assert og.verbs()[-1] == ("create", second.alert_id)
    assert second.alert_id in t.told


def wait_for(predicate, timeout_s: float, what: str) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def test_a_tap_with_no_broker_pages_what_the_store_holds(
    monkeypatch: pytest.MonkeyPatch, store: Store
) -> None:
    """The broker is down (nothing listens on the rabbit URL) and the
    prober has written BrokerUnreachable to the store: the tap's first
    pass creates it in Opsgenie, with no broker involved."""
    monkeypatch.setenv("GWALERTER_RABBIT__URL", "amqp://u:p@localhost:1/d1__1")
    settings = AlerterSettings.load()
    word = Alert(
        src="d1.alerts",
        category=AlertCategory.PlatformService,
        kind=PlatformAlertKind.BrokerUnreachable,
        state=AlertState.Firing,
        subject="localhost:5671",
        alert_id=str(uuid.uuid4()),
        raised_ms=T0,
        summary="Broker door localhost:5671 failed 3 probes running",
        evidence=[],
    )
    store.raise_alert(word)
    og = RecordingOpsgenie()
    t = tap(settings, store, og)
    thread = threading.Thread(target=t.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: bool(og.requests), 5, "the create in Opsgenie")
        assert og.verbs() == [("create", word.alert_id)]
    finally:
        t.stop()
        thread.join(timeout=5)
    assert not thread.is_alive()


def silences(*aliases: str) -> HackAlertSilences:
    return HackAlertSilences(src="d1.alerts", silenced_g_node_aliases=list(aliases))


def test_a_silenced_house_is_not_paged_until_lifted(
    settings: AlerterSettings, store: Store
) -> None:
    """A house on the silence list keeps its open alert out of Opsgenie;
    dropping it from the list pages the alert on the next pass. The store
    still holds the alert open throughout."""
    word = firing()
    store.raise_alert(word)
    store.replace_silences(silences(f"{HOUSE}.ta"))
    og = RecordingOpsgenie()
    t = tap(settings, store, og)
    t.reconcile()
    assert og.verbs() == []
    assert [a.alert_id for a in store.open_alerts()] == [word.alert_id]
    store.replace_silences(silences())
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]


def test_silencing_a_paged_house_closes_its_page(
    settings: AlerterSettings, store: Store
) -> None:
    """A page already open when the house is silenced is closed, with a
    note saying the house is silenced, while the alert stays open in the
    store; a service alert is never silenced by a house list."""
    word = firing()
    store.raise_alert(word)
    og = RecordingOpsgenie()
    t = tap(settings, store, og)
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]
    store.replace_silences(silences(f"{HOUSE}.ta"))
    t.reconcile()
    assert og.verbs()[-1] == ("close", word.alert_id)
    assert og.requests[-1].body["note"] == (
        f"Silenced after {minutes(60_000)} ({HOUSE}.ta)"
    )
    assert t.told == set()
    assert [a.alert_id for a in store.open_alerts()] == [word.alert_id]


def test_the_told_set_is_what_opsgenie_lists(
    settings: AlerterSettings, store: Store
) -> None:
    """A tap restart: Opsgenie already holds the store's open alert from
    the earlier tap, so the first pass lists it and creates nothing."""
    word = firing()
    store.raise_alert(word)
    og = RecordingOpsgenie()
    og.holds(word)
    t = tap(settings, store, og)
    t.reconcile()
    assert og.verbs() == []
    assert t.told == {word.alert_id}
    store.clear_alert(resolved(word))
    t.reconcile()
    assert og.verbs() == [("close", word.alert_id)]


def test_an_alias_opsgenie_holds_that_the_store_does_not_is_closed(
    settings: AlerterSettings, store: Store
) -> None:
    """Opsgenie lists an alert from this alerter that the store neither
    holds open nor has resolved: the alerter owns open and closed, so it
    is closed with a note saying why."""
    stray = firing()
    og = RecordingOpsgenie()
    og.holds(stray)
    t = tap(settings, store, og)
    t.reconcile()
    assert og.verbs() == [("close", stray.alert_id)]
    assert og.requests[-1].body["note"] == NOT_IN_STORE_NOTE
    assert t.told == set()


def test_a_close_made_by_hand_is_undone_while_the_condition_holds(
    settings: AlerterSettings, store: Store
) -> None:
    """A person closes the alert in Opsgenie while the store still holds
    it open: the next pass no longer lists it and creates it again."""
    word = firing()
    store.raise_alert(word)
    og = RecordingOpsgenie()
    t = tap(settings, store, og)
    t.reconcile()
    og.open.pop(word.alert_id)
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id), ("create", word.alert_id)]


def test_a_failed_listing_keeps_the_told_set(
    settings: AlerterSettings, store: Store
) -> None:
    """Opsgenie refuses the listing: the told set stands as creates and
    closes left it (empty at boot), so the first pass creates and the next
    does not create again."""
    word = firing()
    store.raise_alert(word)
    og = RecordingOpsgenie()
    og.listing_fails = True
    t = tap(settings, store, og)
    t.reconcile()
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]
    assert t.told == {word.alert_id}


def test_listed_alert_reads_opsgenies_shape() -> None:
    word = firing()
    body = create_body(to_opsgenie(word, display_name="Spruce", team_id=TEAM))
    item = listing_item(word.alert_id, body)
    item["acknowledged"] = True
    item["report"] = {"ackTime": 1792090900000, "acknowledgedBy": "jess@example.org"}
    listed = listed_alert(item)
    assert listed.alias == word.alert_id
    assert listed.opsgenie_id == f"id-{word.alert_id[:8]}"
    assert listed.tiny_id == "41"
    assert listed.message.startswith("[spruce] [NoData] ")
    assert listed.status is OpsgenieAlertStatus.Open
    assert listed.acknowledged is True
    assert listed.acknowledged_by == "jess@example.org"
    assert listed.closed_by is None
    assert listed.count == 1
    assert listed.priority is OpsgeniePriority.P1
    assert listed.source == "d1.alerts"
    assert listed.tags == ["House", "NoData"]
    assert listed.created_ms == listed.last_occurred_ms == 1792090811482
