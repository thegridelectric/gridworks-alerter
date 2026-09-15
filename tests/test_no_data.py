"""The NoData rule: a silent tracked house raises once at the threshold,
the first arrival clears it with that arrival as evidence, a restart
neither re-raises nor forgets, and boot time counts as heard."""

from __future__ import annotations

import json

from gwbase.actor_base import OnSendMessageDiagnostic

from gwalerter.no_data import NoDataRule
from gwalerter.sema.codec import default_codec
from gwalerter.sema.enums import HouseAlertKind
from gwalerter.sema.types import GNodeForest, HouseAlert, HouseAlertCleared, ReportEvent
from gwalerter.store import Store
from tests.conftest import FLEET_ROOT, forest, house_nodes, report_event
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


def rule(store: Store, boot_ms: int = BOOT_MS) -> NoDataRule:
    return NoDataRule(
        store, src="d1.alerts", silence_ms=SILENCE_MS, heard_floor_ms=boot_ms
    )


def test_silent_house_raises_once_at_the_threshold(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    assert r.evaluate(T0 + SILENCE_MS - 1) == []
    (alert,) = r.evaluate(T0 + SILENCE_MS)
    assert alert.about_g_node_alias == f"{HOUSE}.ta"
    assert alert.kind is HouseAlertKind.NoData
    assert alert.raised_ms == T0 + SILENCE_MS
    assert f"{HOUSE}.ta" in alert.summary
    assert r.evaluate(T0 + 2 * SILENCE_MS) == []
    assert store.open_alert(f"{HOUSE}.ta", HouseAlertKind.NoData) == alert


def test_a_heard_house_is_silent_from_its_last_arrival(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    heard = T0 + 300_000
    store.record_report(spruce_report(), received_ms=heard)
    assert r.evaluate(heard + SILENCE_MS - 1) == []
    (alert,) = r.evaluate(heard + SILENCE_MS)
    assert alert.raised_ms == heard + SILENCE_MS


def test_arrival_clears_with_the_arrival_as_evidence(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    (alert,) = r.evaluate(T0 + SILENCE_MS)
    report = spruce_report()
    arrival = T0 + SILENCE_MS + 1_000
    store.record_report(report, received_ms=arrival)
    cleared = r.on_arrival(
        report.src, arrival_ms=arrival, evidence=report.report.channel_reading_list
    )
    assert isinstance(cleared, HouseAlertCleared)
    assert cleared.alert_id == alert.alert_id
    assert cleared.cleared_ms == arrival
    assert cleared.evidence == report.report.channel_reading_list
    assert store.open_alert(f"{HOUSE}.ta", HouseAlertKind.NoData) is None
    # A second arrival has nothing to clear; a new silence is a new alert.
    assert r.on_arrival(report.src, arrival_ms=arrival + 1, evidence=[]) is None
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
    cleared = restarted.on_arrival(
        f"{HOUSE}.scada", arrival_ms=T0 + 4 * SILENCE_MS, evidence=[]
    )
    assert cleared is not None and cleared.alert_id == alert.alert_id


def test_boot_time_counts_as_heard(store: Store) -> None:
    tracked_spruce(store)
    boot = T0 + 10 * SILENCE_MS
    r = rule(store, boot_ms=boot)
    assert r.evaluate(boot + SILENCE_MS - 1) == []
    assert len(r.evaluate(boot + SILENCE_MS)) == 1


def test_untracked_house_is_neither_raised_nor_cleared(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    other = f"{FLEET_ROOT}.willow.scada"
    assert r.on_arrival(other, arrival_ms=T0, evidence=[]) is None
    (alert,) = r.evaluate(T0 + SILENCE_MS)
    assert alert.about_g_node_alias == f"{HOUSE}.ta"


def test_actor_broadcasts_the_raise_and_the_clear(settings, store: Store) -> None:
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
    raised = default_codec.from_dict(json.loads(raise_body), expect=HouseAlert)
    assert raised.kind is HouseAlertKind.NoData
    assert raise_key.endswith(f"{HOUSE}.ta")
    envelope, body = wrapped("report.event", spruce_report().to_dict())
    a.dispatch_message(envelope=envelope, body=body)
    (_clear_key, clear_body) = sent[1]
    cleared = default_codec.from_dict(json.loads(clear_body), expect=HouseAlertCleared)
    assert cleared.alert_id == raised.alert_id
    assert store.open_alert(f"{HOUSE}.ta", HouseAlertKind.NoData) is None
