"""The critical-glitch rule: a Critical glitch from a tracked scada raises
once with the glitch's summary, a Warning does not, a repeat inside the
hold extends it, a different summary supersedes it, the hold passing
resolves it, and an untracked scada is ignored. The actor relays a glitch
heard on the broker through the same rule."""

from __future__ import annotations

from gwalerter.critical_glitch import CriticalGlitchRule
from gwalerter.duration import minutes
from gwalerter.sema.codec import default_codec
from gwalerter.sema.enums import AlertCategory, AlertState, HouseAlertKind
from gwalerter.sema.types import Alert, Glitch, GNodeForest
from gwalerter.store import Store
from tests.conftest import FLEET_ROOT, forest, house_nodes
from tests.test_actor import actor, wrapped

HOUSE = f"{FLEET_ROOT}.spruce"
TA = f"{HOUSE}.ta"
SCADA = f"{HOUSE}.scada"
BOOT_MS = 1_800_000_000_000
HOLD_MS = 7_200_000
T0 = BOOT_MS


def tracked_spruce(store: Store) -> None:
    store.upsert_forest(
        default_codec.from_dict(
            forest(house_nodes(HOUSE), [FLEET_ROOT], BOOT_MS), expect=GNodeForest
        )
    )


def glitch_dict(
    summary: str = "Store Pump Failed!!",
    *,
    level: str = "Critical",
    src: str = SCADA,
    created_ms: int = T0,
) -> dict:
    return {
        "FromGNodeAlias": src,
        "Node": "la",
        "Type": level,
        "Summary": summary,
        "Details": "Store Pump doctor tried 3 times; manual intervention required",
        "CreatedMs": created_ms,
        "TypeName": "glitch",
        "Version": "000",
    }


def glitch(**kwargs: object) -> Glitch:
    return default_codec.from_dict(glitch_dict(**kwargs), expect=Glitch)  # type: ignore[arg-type]


def rule(store: Store) -> CriticalGlitchRule:
    return CriticalGlitchRule(store, src="d1.alerts", hold_ms=HOLD_MS)


def open_glitch(store: Store) -> Alert | None:
    return store.open_alert(
        AlertCategory.House, HouseAlertKind.CriticalGlitch, about_g_node_alias=TA
    )


def test_a_critical_glitch_raises_with_its_summary(store: Store) -> None:
    tracked_spruce(store)
    (alert,) = rule(store).on_glitch(glitch(), received_ms=T0)
    assert alert.category is AlertCategory.House
    assert alert.kind is HouseAlertKind.CriticalGlitch
    assert alert.state is AlertState.Firing
    assert alert.about_g_node_alias == TA
    assert alert.raised_ms == T0
    assert alert.summary == f"Store Pump Failed!! [la] ({TA})"
    assert open_glitch(store) == alert


def test_a_warning_does_not_page(store: Store) -> None:
    tracked_spruce(store)
    assert rule(store).on_glitch(glitch(level="Warning"), received_ms=T0) == []
    assert open_glitch(store) is None


def test_an_untracked_scada_is_ignored(store: Store) -> None:
    tracked_spruce(store)
    stranger = f"{FLEET_ROOT}.elsewhere.scada"
    assert rule(store).on_glitch(glitch(src=stranger), received_ms=T0) == []
    assert store.open_alerts() == []


def test_a_repeat_inside_the_hold_extends_without_re_raising(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    (alert,) = r.on_glitch(glitch(), received_ms=T0)
    repeat_ms = T0 + HOLD_MS - 1_000
    assert r.on_glitch(glitch(), received_ms=repeat_ms) == []
    assert r.evaluate(T0 + HOLD_MS) == []
    (resolved,) = r.evaluate(repeat_ms + HOLD_MS)
    assert resolved.alert_id == alert.alert_id


def test_the_hold_passing_resolves(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    (alert,) = r.on_glitch(glitch(), received_ms=T0)
    assert r.evaluate(T0 + HOLD_MS - 1) == []
    (resolved,) = r.evaluate(T0 + HOLD_MS)
    assert resolved.state is AlertState.Resolved
    assert resolved.alert_id == alert.alert_id
    assert resolved.raised_ms == alert.raised_ms
    assert resolved.resolved_ms == T0 + HOLD_MS
    assert resolved.summary == f"no repeat for {minutes(HOLD_MS)} ({TA})"
    assert open_glitch(store) is None
    # The same summary after the hold is a new alert.
    (again,) = r.on_glitch(glitch(), received_ms=T0 + HOLD_MS + 1)
    assert again.alert_id != alert.alert_id


def test_a_different_summary_supersedes_the_open_alert(store: Store) -> None:
    tracked_spruce(store)
    r = rule(store)
    (first,) = r.on_glitch(glitch(), received_ms=T0)
    superseded, second = r.on_glitch(
        glitch(summary="zone-freezing"), received_ms=T0 + 60_000
    )
    assert superseded.state is AlertState.Resolved
    assert superseded.alert_id == first.alert_id
    assert superseded.summary == f"superseded by zone-freezing ({TA})"
    assert second.state is AlertState.Firing
    assert second.alert_id != first.alert_id
    assert second.summary == f"zone-freezing [la] ({TA})"
    assert open_glitch(store) == second


def test_restart_counts_the_hold_from_the_raise(store: Store) -> None:
    tracked_spruce(store)
    (alert,) = rule(store).on_glitch(glitch(), received_ms=T0)
    fresh = rule(store)
    assert fresh.evaluate(T0 + HOLD_MS - 1) == []
    (resolved,) = fresh.evaluate(T0 + HOLD_MS)
    assert resolved.alert_id == alert.alert_id


def test_the_actor_relays_a_glitch_from_the_broker(settings, store: Store) -> None:
    tracked_spruce(store)
    a = actor(settings, store)
    envelope, body = wrapped("glitch", glitch_dict())
    a.dispatch_message(envelope=envelope, body=body)
    alert = open_glitch(store)
    assert alert is not None
    assert alert.summary == f"Store Pump Failed!! [la] ({TA})"
    envelope, body = wrapped("glitch", glitch_dict(level="Warning"))
    a.dispatch_message(envelope=envelope, body=body)
    assert [x.alert_id for x in store.open_alerts()] == [alert.alert_id]
