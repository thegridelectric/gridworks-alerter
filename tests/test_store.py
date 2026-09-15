"""The store keeps layouts, a readings window, and last-heard per house."""

from __future__ import annotations

from pathlib import Path

from gwalerter.sema.codec import default_codec
from gwalerter.sema.types import LayoutLite, ReportEvent
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
    assert house.layout_received_ms is None
    latest = store.latest_reading(event.src, first.channel_name)
    assert latest is not None
    assert latest.value == first.value_list[-1]
    assert latest.read_ms == first.scada_read_time_unix_ms_list[-1]
    assert len(store.readings_since(event.src, first.channel_name, 0)) == len(
        first.value_list
    )


def test_readings_older_than_the_window_leave(store: Store) -> None:
    event = report_event()
    first = event.report.channel_reading_list[0]
    newest = max(first.scada_read_time_unix_ms_list)
    store.record_report(event, received_ms=newest)
    assert store.readings_since(event.src, first.channel_name, 0)
    store.record_report(event, received_ms=newest + store.readings_window_s * 1000 + 1)
    # the second append is itself older than the window, so nothing remains
    assert store.readings_since(event.src, first.channel_name, 0) == []


def test_layout_round_trips_and_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "alerter.sqlite"
    store = Store.open(path, readings_window_s=60)
    layout = layout_lite()
    store.record_layout(layout, received_ms=1_800_000_000_000)
    store.close()

    reopened = Store.open(path, readings_window_s=60)
    (house,) = reopened.houses()
    assert house.alias == layout.from_g_node_alias
    assert house.layout_received_ms == 1_800_000_000_000
    assert house.last_heard_ms == 1_800_000_000_000
    assert reopened.layout(layout.from_g_node_alias) == layout
    assert reopened.layout("d1.nowhere.scada") is None


def test_last_heard_never_moves_backwards(store: Store) -> None:
    event = report_event()
    store.record_report(event, received_ms=1_800_000_002_000)
    store.record_report(event, received_ms=1_800_000_001_000)
    (house,) = store.houses()
    assert house.last_heard_ms == 1_800_000_002_000
