"""The no-data rule: a silent tracked house raises once at the threshold,
a report in the store resolves it on the tick with that report's readings
as evidence, a restart neither re-raises nor forgets, and boot time counts
as heard. Both records are one `gw.alert` word, whose axioms the codec
checks."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from gwbase.actor_base import OnSendMessageDiagnostic

from gwalerter.duration import minutes
from gwalerter.no_data import NYQUIST, SILENCE_KINDS, SUMMARIES, NoDataRule
from gwalerter.sema.base import SemaError
from gwalerter.sema.codec import default_codec
from gwalerter.sema.enums import (
    AlertCategory,
    AlertState,
    FleetAlertKind,
    HouseAlertKind,
    PlatformAlertKind,
)
from gwalerter.sema.types import Alert, GNodeForest, LayoutLite, ReportEvent
from gwalerter.store import Store
from tests.conftest import FLEET_ROOT, forest, house_nodes, report_event, sample
from tests.test_actor import actor, wrapped

HOUSE = f"{FLEET_ROOT}.spruce"
BOOT_MS = 1_800_000_000_000
SILENCE_MS = 600_000
T0 = BOOT_MS


def tracked_spruce(store: Store) -> None:
    store.upsert_forest(
        default_codec.from_dict(
            forest(house_nodes(HOUSE), [FLEET_ROOT], BOOT_MS), expect=GNodeForest
        )
    )


def spruce_report(read_ms: int = BOOT_MS) -> ReportEvent:
    return report_event(f"{HOUSE}.scada", HOUSE, read_ms=read_ms)


def spruce_layout() -> LayoutLite:
    return default_codec.from_dict(
        {**sample("layout.lite.012.json"), "FromGNodeAlias": f"{HOUSE}.scada"},
        expect=LayoutLite,
    )


def open_no_data(store: Store) -> Alert | None:
    """Spruce's open silence alert of any kind."""
    for kind in SILENCE_KINDS:
        alert = store.open_alert(
            AlertCategory.House, kind, about_g_node_alias=f"{HOUSE}.ta"
        )
        if alert is not None:
            return alert
    return None


HEARD_PERIOD_MS = 60_000
HEARD_FRESH_MS = int(HEARD_PERIOD_MS * NYQUIST)
LTN = HOUSE
SCADA = f"{HOUSE}.scada"


def rule(store: Store, boot_ms: int = BOOT_MS) -> NoDataRule:
    return NoDataRule(
        store,
        src="d1.alerts",
        silence_ms=SILENCE_MS,
        heard_floor_ms=boot_ms,
        heard_period_ms=HEARD_PERIOD_MS,
    )


def test_silent_house_raises_once_at_the_threshold(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    assert r.evaluate(T0 + SILENCE_MS - 1) == []
    (alert,) = r.evaluate(T0 + SILENCE_MS)
    assert alert.about_g_node_alias == f"{HOUSE}.ta"
    assert alert.category is AlertCategory.House
    assert alert.kind is HouseAlertKind.ScadaSilent
    assert alert.state is AlertState.Firing
    assert alert.raised_ms == T0 + SILENCE_MS
    assert alert.resolved_ms is None
    assert f"{HOUSE}.ta" in alert.summary
    assert r.evaluate(T0 + 2 * SILENCE_MS) == []
    assert open_no_data(store) == alert


def test_a_heard_house_is_silent_from_its_last_arrival(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    heard = T0 + 300_000
    store.record_report(spruce_report(), received_ms=heard)
    assert r.evaluate(heard + SILENCE_MS - 1) == []
    (alert,) = r.evaluate(heard + SILENCE_MS)
    assert alert.raised_ms == heard + SILENCE_MS


def test_a_report_in_the_store_resolves_on_the_tick(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    (alert,) = r.evaluate(T0 + SILENCE_MS)
    report = spruce_report()
    arrival = T0 + SILENCE_MS + 1_000
    store.record_report(report, received_ms=arrival)
    (resolved,) = r.evaluate(arrival + 5_000)
    assert resolved.state is AlertState.Resolved
    assert resolved.alert_id == alert.alert_id
    assert resolved.raised_ms == alert.raised_ms
    assert resolved.resolved_ms == arrival
    assert resolved.evidence == report.report.channel_reading_list
    assert open_no_data(store) is None
    assert (
        resolved.summary
        == f"data again after {minutes(SILENCE_MS + 1_000)} ({HOUSE}.ta)"
    )
    # The next tick has nothing to resolve; a new silence is a new alert.
    assert r.evaluate(arrival + 6_000) == []
    (again,) = r.evaluate(arrival + SILENCE_MS)
    assert again.alert_id != alert.alert_id
    assert [a.alert_id for a in store.alerts(f"{HOUSE}.ta")] == [
        alert.alert_id,
        again.alert_id,
    ]


def test_restart_neither_re_raises_nor_forgets(store: Store) -> None:
    tracked_spruce(store)
    (alert,) = rule(store).evaluate(T0 + SILENCE_MS)
    restarted = rule(store, boot_ms=T0 + 2 * SILENCE_MS)
    assert restarted.evaluate(T0 + 4 * SILENCE_MS) == []
    store.record_report(spruce_report(), received_ms=T0 + 4 * SILENCE_MS)
    (resolved,) = restarted.evaluate(T0 + 4 * SILENCE_MS + 1_000)
    assert resolved.state is AlertState.Resolved
    assert resolved.alert_id == alert.alert_id


def test_a_layout_is_no_sign_of_life(store: Store) -> None:
    """A layout neither defers the raise nor resolves the alert; it is
    stored for meaning. Only a report is heard."""
    tracked_spruce(store)
    r = rule(store)
    store.record_layout(spruce_layout(), received_ms=T0 + SILENCE_MS - 1_000)
    (alert,) = r.evaluate(T0 + SILENCE_MS)
    assert alert.raised_ms == T0 + SILENCE_MS
    store.record_layout(spruce_layout(), received_ms=T0 + SILENCE_MS + 1_000)
    assert r.evaluate(T0 + SILENCE_MS + 2_000) == []
    assert open_no_data(store) == alert


def test_boot_time_counts_as_heard(store: Store) -> None:
    tracked_spruce(store)
    boot = T0 + 10 * SILENCE_MS
    r = rule(store, boot_ms=boot)
    assert r.evaluate(boot + SILENCE_MS - 1) == []
    assert len(r.evaluate(boot + SILENCE_MS)) == 1


def broker_down(store: Store, *, raised_ms: int) -> Alert:
    """What the prober writes when a door fails three probes running."""
    alert = Alert(
        src="d1.alerts",
        category=AlertCategory.PlatformService,
        kind=PlatformAlertKind.BrokerUnreachable,
        state=AlertState.Firing,
        subject="hw1-1.electricity.works:5671",
        alert_id=str(uuid.uuid4()),
        raised_ms=raised_ms,
        summary="Broker door hw1-1.electricity.works:5671 failed 3 probes running",
        evidence=[],
    )
    store.raise_alert(alert)
    return alert


def broker_back(store: Store, firing: Alert, *, resolved_ms: int) -> None:
    store.clear_alert(
        Alert.from_dict({
            **firing.to_dict(),
            "State": AlertState.Resolved.value,
            "ResolvedMs": resolved_ms,
            "Summary": "Broker door answered again",
        })
    )


def test_no_data_holds_while_the_broker_is_down_and_refloors_after(
    store: Store,
) -> None:
    """Every house is silent for one reason, and the prober's alert says
    so: no silence alert while BrokerUnreachable is open. When it resolves, each
    house gets one threshold to reconnect before it pages."""
    tracked_spruce(store)
    r = rule(store)
    down = broker_down(store, raised_ms=T0 + SILENCE_MS // 2)
    assert r.evaluate(T0 + SILENCE_MS) == []
    assert r.evaluate(T0 + 5 * SILENCE_MS) == []
    assert open_no_data(store) is None
    back = T0 + 6 * SILENCE_MS
    broker_back(store, down, resolved_ms=back)
    assert r.evaluate(back) == []
    assert r.evaluate(back + SILENCE_MS - 1) == []
    (alert,) = r.evaluate(back + SILENCE_MS)
    assert alert.kind is HouseAlertKind.ScadaSilent
    assert alert.summary == f"scada silent for {minutes(SILENCE_MS)} ({HOUSE}.ta)"


def test_an_open_no_data_survives_the_broker_going_down(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    (alert,) = r.evaluate(T0 + SILENCE_MS)
    broker_down(store, raised_ms=T0 + 2 * SILENCE_MS)
    assert r.evaluate(T0 + 3 * SILENCE_MS) == []
    assert open_no_data(store) is not None
    store.record_report(spruce_report(), received_ms=T0 + 3 * SILENCE_MS)
    (resolved,) = r.evaluate(T0 + 3 * SILENCE_MS + 1_000)
    assert resolved.state is AlertState.Resolved
    assert resolved.alert_id == alert.alert_id


def test_untracked_house_is_neither_raised_nor_resolved(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    other = f"{FLEET_ROOT}.willow"
    store.record_report(
        report_event(f"{other}.scada", other, read_ms=T0), received_ms=T0
    )
    (alert,) = r.evaluate(T0 + SILENCE_MS)
    assert alert.about_g_node_alias == f"{HOUSE}.ta"
    assert r.evaluate(T0 + 2 * SILENCE_MS) == []


def test_actor_broadcasts_firing_and_resolved(settings, store: Store) -> None:
    tracked_spruce(store)
    a = actor(settings, store)  # clock fixed at BOOT_MS
    sent: list[tuple[str, bytes]] = []

    def capture(*, envelope, body, correlation_id=None):
        sent.append((envelope.routing_key, body))
        return OnSendMessageDiagnostic.MESSAGE_SENT

    a.send = capture  # type: ignore[method-assign]
    a.clock_ms = lambda: BOOT_MS + SILENCE_MS
    a.evaluate_detectors()
    (raise_key, raise_body) = sent[0]
    raised = default_codec.from_dict(json.loads(raise_body), expect=Alert)
    assert raised.state is AlertState.Firing
    assert raised.kind is HouseAlertKind.ScadaSilent
    assert raise_key.endswith(f"{HOUSE}.ta")
    a.clock_ms = lambda: BOOT_MS + SILENCE_MS + 1_000
    envelope, body = wrapped("report.event", spruce_report().to_dict())
    a.dispatch_message(envelope=envelope, body=body)
    assert len(sent) == 1
    a.clock_ms = lambda: BOOT_MS + SILENCE_MS + 2_000
    a.evaluate_detectors()
    (resolve_key, resolve_body) = sent[1]
    resolved = default_codec.from_dict(json.loads(resolve_body), expect=Alert)
    assert resolved.state is AlertState.Resolved
    assert resolved.alert_id == raised.alert_id
    assert resolve_key.endswith(f"{HOUSE}.ta")
    assert open_no_data(store) is None


def test_resumed_hearing_gives_each_house_one_threshold(settings, store: Store) -> None:
    """The actor was not consuming across the threshold (the broker went
    away and came back): on resuming it raises nothing, since the silence
    was its own deafness, and a house pages only a full threshold later."""
    tracked_spruce(store)
    a = actor(settings, store)  # clock fixed at BOOT_MS
    sent: list[bytes] = []

    def capture(*, envelope, body, correlation_id=None):
        sent.append(body)
        return OnSendMessageDiagnostic.MESSAGE_SENT

    a.send = capture  # type: ignore[method-assign]
    a._live_channel = lambda: SimpleNamespace(queue_bind=lambda *_a, **_k: None)  # type: ignore[method-assign]
    resumed = BOOT_MS + 3 * SILENCE_MS
    a.clock_ms = lambda: resumed
    a.local_rabbit_startup()
    # Startup announces the silences snapshot, never an alert.
    assert [json.loads(b)["TypeName"] for b in sent] == ["gw.hack.alert.silences"]
    sent.clear()
    a.evaluate_detectors()
    assert sent == []
    a.clock_ms = lambda: resumed + SILENCE_MS - 1
    a.evaluate_detectors()
    assert sent == []
    a.clock_ms = lambda: resumed + SILENCE_MS
    a.evaluate_detectors()
    (body,) = sent
    raised = default_codec.from_dict(json.loads(body), expect=Alert)
    assert raised.kind is HouseAlertKind.ScadaSilent
    assert raised.summary == f"scada silent for {minutes(SILENCE_MS)} ({HOUSE}.ta)"


def test_firing_and_resolved_round_trip_under_the_axioms(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    (firing,) = r.evaluate(T0 + SILENCE_MS)
    store.record_report(spruce_report(), received_ms=T0 + SILENCE_MS + 1_000)
    (resolved,) = r.evaluate(T0 + SILENCE_MS + 2_000)
    for record in (firing, resolved):
        wire = record.to_dict()
        assert default_codec.from_dict(wire, expect=Alert) == record
    wire = firing.to_dict()
    # Axiom 1: a House alert takes its Kind from gw.house.alert.kind.
    with pytest.raises(SemaError, match="CategoryKindConsistency"):
        default_codec.from_dict(
            {**wire, "Kind": FleetAlertKind.AllHousesSilent.value}, expect=Alert
        )
    # Axiom 3: Resolved has ResolvedMs, Firing does not.
    with pytest.raises(SemaError, match="ResolvedTime"):
        default_codec.from_dict({**wire, "State": "Resolved"}, expect=Alert)
    with pytest.raises(SemaError, match="ResolvedTime"):
        default_codec.from_dict(
            {**wire, "ResolvedMs": T0 + 2 * SILENCE_MS}, expect=Alert
        )


# -- the cause clause -------------------------------------------------------


@pytest.mark.parametrize(
    ("scada", "ltn", "kind"),
    [
        (False, False, HouseAlertKind.ScadaSilent),
        (False, True, HouseAlertKind.ScadaSilent),
        (True, False, HouseAlertKind.LtnSilent),
        (True, True, HouseAlertKind.NoData),
    ],
)
def test_the_kind_names_the_cause(
    store: Store, scada: bool, ltn: bool, kind: HouseAlertKind
) -> None:
    """Each party heard at all inside the window: the kind says which is
    missing, the summary the silence in minutes with the alias last. A
    silent scada is ScadaSilent whether or not the LTN is silent too.
    Envelope arrivals, no body decoded, no type looked at."""
    tracked_spruce(store)
    now = T0 + SILENCE_MS
    if scada:
        store.record_arrival(SCADA, received_ms=now - 30_000)
    if ltn:
        store.record_arrival(LTN, received_ms=now - 60_000)
    (alert,) = rule(store).evaluate(now)
    assert alert.kind is kind
    clause = SUMMARIES[kind].format(d=minutes(SILENCE_MS))
    assert alert.summary == f"{clause} ({HOUSE}.ta)"


def test_the_ltn_line_teaches_why_the_scada_is_quiet(store: Store) -> None:
    tracked_spruce(store)
    now = T0 + SILENCE_MS
    store.record_arrival(SCADA, received_ms=now - 30_000)
    (alert,) = rule(store).evaluate(now)
    assert alert.summary == (
        f"ltn silent for {minutes(SILENCE_MS)}; scada does not send up reports "
        f"with no ltn ({HOUSE}.ta)"
    )


def test_a_party_outside_the_window_is_silent(store: Store) -> None:
    """A scada last heard a Nyquist window ago (2.1 periods) is silent,
    even though that is well inside the NoData threshold."""
    tracked_spruce(store)
    now = T0 + SILENCE_MS
    store.record_arrival(SCADA, received_ms=now - HEARD_FRESH_MS)
    store.record_arrival(LTN, received_ms=now - 60_000)
    (alert,) = rule(store).evaluate(now)
    assert alert.kind is HouseAlertKind.ScadaSilent


def test_the_open_alert_kind_follows_the_cause(store: Store) -> None:
    """The cause is re-read on every tick: when it changes, the open
    alert's Firing record is rewritten in the store under the same
    AlertId with the new kind and returned again, and nothing new is
    raised. The LTN joining or leaving the silence while the scada stays
    silent is not a change."""
    tracked_spruce(store)
    r = rule(store)
    now = T0 + SILENCE_MS
    (firing,) = r.evaluate(now)
    assert firing.kind is HouseAlertKind.ScadaSilent
    assert r.evaluate(now + 10_000) == []
    store.record_arrival(LTN, received_ms=now + 15_000)
    assert r.evaluate(now + 20_000) == []  # still the scada's silence
    # The LTN falls out of its fresh window and the scada speaks: the
    # cause is now the LTN's silence.
    later = now + 15_000 + HEARD_FRESH_MS + 10_000
    store.record_arrival(SCADA, received_ms=later - 5_000)
    (updated,) = r.evaluate(later)
    assert updated.alert_id == firing.alert_id
    assert updated.state is AlertState.Firing
    assert updated.raised_ms == firing.raised_ms
    assert updated.kind is HouseAlertKind.LtnSilent
    assert open_no_data(store) == updated
    assert r.evaluate(later + 10_000) == []


def test_actor_counts_every_envelope_under_the_roots(settings, store: Store) -> None:
    """A message of an untracked type is never decoded, but its envelope
    is an arrival for the alias that sent it; an alias outside the fleet
    roots is not counted."""
    tracked_spruce(store)
    a = actor(settings, store)
    envelope, body = wrapped("gridworks.ping", {"TypeName": "gridworks.ping"}, src=LTN)
    a.dispatch_message(envelope=envelope, body=body)
    assert store.last_arrival(LTN) == a.clock_ms()
    stranger = "d1.elsewhere"
    envelope, body = wrapped(
        "gridworks.ping", {"TypeName": "gridworks.ping"}, src=stranger
    )
    a.dispatch_message(envelope=envelope, body=body)
    assert store.last_arrival(stranger) is None
