"""The store keeps layouts, a readings window, and last-heard per house."""

from __future__ import annotations

from pathlib import Path

from gwalerter.sema.codec import default_codec
from gwalerter.sema.types import HackAlertSilences, LayoutLite, ReportEvent
from gwalerter.store import Store
from tests.conftest import sample


def report_event() -> ReportEvent:
    return default_codec.from_dict(sample("report.event.004.json"), expect=ReportEvent)


def layout_lite() -> LayoutLite:
    return default_codec.from_dict(sample("layout.lite.012.json"), expect=LayoutLite)


def test_report_marks_house_heard_and_keeps_readings(store: Store) -> None:
    event = report_event()
    first = event.report.channel_reading_list[0]
    received_ms = max(first.scada_read_time_unix_ms_list)
    store.record_report(event, received_ms=received_ms)
    (house,) = store.houses()
    assert house.alias == event.src
    assert house.last_heard_ms == received_ms
    assert len(store.readings_since(event.src, first.channel_name, 0)) == len(
        first.value_list
    )


def test_readings_older_than_the_window_leave(store: Store) -> None:
    event = report_event()
    first = event.report.channel_reading_list[0]
    newest = max(first.scada_read_time_unix_ms_list)
    store.record_report(event, received_ms=newest)
    n = len(store.readings_since(event.src, first.channel_name, 0))
    assert n == len(first.value_list)
    store.record_report(event, received_ms=newest + store.readings_window_s * 1000 + 1)
    # the first arrival is now older than the window; only the second stays
    assert len(store.readings_since(event.src, first.channel_name, 0)) == n


def test_layout_round_trips_and_survives_reopen(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'alerter.sqlite'}"
    store = Store.open(url, readings_window_s=60, fleet_roots=[])
    layout = layout_lite()
    store.record_layout(layout, received_ms=1_800_000_000_000)
    store.close()

    reopened = Store.open(url, readings_window_s=60, fleet_roots=[])
    assert reopened.houses() == []
    assert reopened.layout(layout.from_g_node_alias) == layout
    assert reopened.layout("d1.nowhere.scada") is None


def test_report_readings_regroup_by_channel(store: Store) -> None:
    event = report_event()
    arrival = 1_800_000_000_000
    store.record_report(event, received_ms=arrival)
    store.record_report(event, received_ms=arrival + 1)
    readings = store.report_readings(event.src, received_ms=arrival)
    assert sorted(r.channel_name for r in readings) == sorted(
        c.channel_name for c in event.report.channel_reading_list
    )
    by_name = {c.channel_name: c for c in event.report.channel_reading_list}
    for got in readings:
        sent = by_name[got.channel_name]
        assert sorted(zip(got.value_list, got.scada_read_time_unix_ms_list)) == sorted(
            zip(sent.value_list, sent.scada_read_time_unix_ms_list)
        )
    assert store.report_readings(event.src, received_ms=arrival - 1) == []


def test_last_heard_never_moves_backwards(store: Store) -> None:
    event = report_event()
    store.record_report(event, received_ms=1_800_000_002_000)
    store.record_report(event, received_ms=1_800_000_001_000)
    (house,) = store.houses()
    assert house.last_heard_ms == 1_800_000_002_000


def test_schema_comes_from_the_migration_chain(store: Store) -> None:
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from gwalerter.store import MIGRATIONS_DIR

    head = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
    with store.engine.connect() as conn:
        (current,) = conn.execute(text("SELECT version_num FROM alembic_version")).one()
    assert current == head


def test_silences_are_replaced_wholesale(store: Store) -> None:
    """The silences table is the latest snapshot's list and nothing else:
    a house missing from the latest list is no longer silenced."""
    elm = "d1.isone.me.versant.keene.elm.ta"
    oak = "d1.isone.me.versant.keene.oak.ta"
    assert store.silenced_aliases() == []
    assert store.replace_silences(
        HackAlertSilences(src="d1.alerts", silenced_g_node_aliases=[elm])
    )
    assert store.silenced_aliases() == [elm]
    assert not store.replace_silences(
        HackAlertSilences(src="d1.alerts", silenced_g_node_aliases=[elm])
    )
    assert store.replace_silences(
        HackAlertSilences(src="d1.alerts", silenced_g_node_aliases=[oak])
    )
    assert store.silenced_aliases() == [oak]
    assert store.replace_silences(
        HackAlertSilences(src="d1.alerts", silenced_g_node_aliases=[])
    )
    assert store.silenced_aliases() == []
