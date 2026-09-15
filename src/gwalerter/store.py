"""The alerter's sqlite store: the latest layout per house, a rolling window
of readings, and when each house was last heard from.

Every row in and out is a typed record. The `HouseRecord` here is the
interim for a sema liveness record word (one per scada: last heard, mode,
live flag); when that word is published the store holds instances of it
and this record goes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import NamedTuple

from pydantic import TypeAdapter

from gwalerter.sema.codec import default_codec
from gwalerter.sema.property_format import (
    LeftRightDot,
    SpaceheatName,
    UTCMilliseconds,
)
from gwalerter.sema.types import LayoutLite, ReportEvent

LRD = TypeAdapter(LeftRightDot)
SH_NAME = TypeAdapter(SpaceheatName)
UTC_MS = TypeAdapter(UTCMilliseconds)

SCHEMA = """
CREATE TABLE IF NOT EXISTS houses (
    alias TEXT PRIMARY KEY,
    last_heard_ms INTEGER,
    layout_json TEXT,
    layout_received_ms INTEGER
);
CREATE TABLE IF NOT EXISTS readings (
    house TEXT NOT NULL,
    channel TEXT NOT NULL,
    value INTEGER NOT NULL,
    read_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_house_channel_ms
    ON readings (house, channel, read_ms);
"""


class Reading(NamedTuple):
    """One reading of one channel: the raw integer value the scada sent
    (its unit and exponent come from the house layout) and when the scada
    read it."""

    channel: SpaceheatName
    value: int
    read_ms: UTCMilliseconds


class HouseRecord(NamedTuple):
    """What the store knows about one scada: when it was last heard from
    (any tracked message), and when its latest layout arrived. `None`
    where the house has never sent that kind of message."""

    alias: LeftRightDot
    last_heard_ms: UTCMilliseconds | None
    layout_received_ms: UTCMilliseconds | None


class Store:
    """sqlite behind a lock: the actor's consumer thread writes, detectors
    read. Open with `Store.open`; `close` when the actor stops."""

    def __init__(self, conn: sqlite3.Connection, *, readings_window_s: int) -> None:
        self.conn = conn
        self.readings_window_s = readings_window_s
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    @classmethod
    def open(cls, path: Path | str, *, readings_window_s: int) -> Store:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        return cls(conn, readings_window_s=readings_window_s)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # -- writes ----------------------------------------------------------

    def record_layout(
        self, layout: LayoutLite, *, received_ms: UTCMilliseconds
    ) -> None:
        """Keep the latest layout per house; it is also a sign of life."""
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO houses (alias, last_heard_ms, layout_json, layout_received_ms)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    last_heard_ms = MAX(COALESCE(last_heard_ms, 0), excluded.last_heard_ms),
                    layout_json = excluded.layout_json,
                    layout_received_ms = excluded.layout_received_ms
                """,
                (
                    layout.from_g_node_alias,
                    received_ms,
                    json.dumps(layout.to_dict()),
                    received_ms,
                ),
            )
            self.conn.commit()

    def record_report(
        self, event: ReportEvent, *, received_ms: UTCMilliseconds
    ) -> None:
        """Append every reading in the report and mark the house heard;
        readings older than the window leave in the same transaction."""
        house = event.src
        rows = [
            (house, readings.channel_name, value, read_ms)
            for readings in event.report.channel_reading_list
            for value, read_ms in zip(
                readings.value_list, readings.scada_read_time_unix_ms_list, strict=True
            )
        ]
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO houses (alias, last_heard_ms) VALUES (?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    last_heard_ms = MAX(COALESCE(last_heard_ms, 0), excluded.last_heard_ms)
                """,
                (house, received_ms),
            )
            self.conn.executemany(
                "INSERT INTO readings (house, channel, value, read_ms) VALUES (?, ?, ?, ?)",
                rows,
            )
            self.conn.execute(
                "DELETE FROM readings WHERE read_ms < ?",
                (received_ms - self.readings_window_s * 1000,),
            )
            self.conn.commit()

    # -- reads -----------------------------------------------------------

    def houses(self) -> list[HouseRecord]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT alias, last_heard_ms, layout_received_ms FROM houses ORDER BY alias"
            ).fetchall()
        return [
            HouseRecord(
                alias=LRD.validate_python(alias),
                last_heard_ms=None if heard is None else UTC_MS.validate_python(heard),
                layout_received_ms=None if got is None else UTC_MS.validate_python(got),
            )
            for alias, heard, got in rows
        ]

    def layout(self, house: LeftRightDot) -> LayoutLite | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT layout_json FROM houses WHERE alias = ?", (house,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return default_codec.from_dict(json.loads(row[0]), expect=LayoutLite)

    def latest_reading(
        self, house: LeftRightDot, channel: SpaceheatName
    ) -> Reading | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT channel, value, read_ms FROM readings
                WHERE house = ? AND channel = ? ORDER BY read_ms DESC LIMIT 1
                """,
                (house, channel),
            ).fetchone()
        return None if row is None else self.reading(row)

    def readings_since(
        self, house: LeftRightDot, channel: SpaceheatName, since_ms: UTCMilliseconds
    ) -> list[Reading]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT channel, value, read_ms FROM readings
                WHERE house = ? AND channel = ? AND read_ms >= ? ORDER BY read_ms
                """,
                (house, channel, since_ms),
            ).fetchall()
        return [self.reading(row) for row in rows]

    def reading(self, row: tuple[str, int, int]) -> Reading:
        channel, value, read_ms = row
        return Reading(
            channel=SH_NAME.validate_python(channel),
            value=value,
            read_ms=UTC_MS.validate_python(read_ms),
        )
