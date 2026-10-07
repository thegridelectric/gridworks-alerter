"""The tap: an alert open in the store creates an Opsgenie alert aliased
by its `AlertId`, its `Resolved` record closes that alias, the reconcile
pass repairs what Opsgenie was not told (a tap restart, a failed post),
a refused post is retried then left for the next pass, and none of it
needs a broker. Opsgenie itself is a recording httpx transport here."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import NamedTuple

import httpx
import pytest

from gwalerter.config import AlerterSettings
from gwalerter.sema.enums import (
    AlertCategory,
    AlertState,
    HouseAlertKind,
    PlatformAlertKind,
)
from gwalerter.sema.types import Alert
from gwalerter.store import Store
from gwalerter.tap import (
    MESSAGE_MAX_CHARS,
    OpsgenieClient,
    Tap,
    close_body,
    create_body,
    house_short_name,
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


class RecordingOpsgenie:
    """An httpx transport standing in for Opsgenie's Alert API: records
    every create and close, checks the key, and refuses the first
    `refuse` connections."""

    def __init__(self, refuse: int = 0) -> None:
        self.requests: list[Request] = []
        self.refuse = refuse

    def transport(self) -> httpx.BaseTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if self.refuse > 0:
                self.refuse -= 1
                raise httpx.ConnectError("connection refused", request=request)
            assert request.headers["Authorization"] == "GenieKey key-0000"
            body = json.loads(request.content)
            if request.url.path == "/v2/alerts":
                self.requests.append(Request("create", body["alias"], body))
            else:
                alias = request.url.path.removeprefix("/v2/alerts/").removesuffix(
                    "/close"
                )
                assert request.url.params["identifierType"] == "alias"
                self.requests.append(Request("close", alias, body))
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
    assert create.message == "[spruce] No data from spruce"
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


def tap(settings: AlerterSettings, store: Store, og: RecordingOpsgenie, clock) -> Tap:
    return Tap(settings=settings, store=store, opsgenie=og.client(), clock=clock)


def test_firing_creates_and_resolved_closes(
    settings: AlerterSettings, store: Store
) -> None:
    og = RecordingOpsgenie()
    t = tap(settings, store, og, time.monotonic)
    word = firing()
    store.raise_alert(word)
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]
    assert word.alert_id in t.told
    store.clear_alert(resolved(word))
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id), ("close", word.alert_id)]
    assert og.requests[-1].body["note"] == "Data from spruce again"
    assert t.told == {}


def test_reconcile_tells_opsgenie_what_the_store_holds_open(
    settings: AlerterSettings, store: Store
) -> None:
    """A tap restart: the store has an open alert Opsgenie may not have
    been told of; the first pass creates it (Opsgenie dedups by alias),
    and a later pass closes it once the store has resolved it, with the
    Resolved summary as the note."""
    word = firing()
    store.raise_alert(word)
    og = RecordingOpsgenie()
    now = [0.0]
    t = tap(settings, store, og, lambda: now[0])
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]
    assert not t.reconcile_due()
    now[0] = settings.tap_reconcile_s
    assert t.reconcile_due()
    t.reconcile()
    assert og.verbs() == [("create", word.alert_id)]  # already told; nothing new
    # The alerter resolves it but the Resolved record never reaches the
    # tap: the next pass closes it anyway.
    store.clear_alert(resolved(word))
    now[0] = 2 * settings.tap_reconcile_s
    t.reconcile()
    assert og.verbs()[-1] == ("close", word.alert_id)
    assert og.requests[-1].body["note"] == "Data from spruce again"
    assert t.told == {}


def test_refused_post_is_retried_then_left_for_reconcile(
    settings: AlerterSettings, store: Store
) -> None:
    og = RecordingOpsgenie(refuse=2)
    t = tap(settings, store, og, time.monotonic)
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
    t = tap(settings, store, og, time.monotonic)
    thread = threading.Thread(target=t.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: bool(og.requests), 5, "the create in Opsgenie")
        assert og.verbs() == [("create", word.alert_id)]
    finally:
        t.stop()
        thread.join(timeout=5)
    assert not thread.is_alive()
