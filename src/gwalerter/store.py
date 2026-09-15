"""The alerter's sqlite store behind SQLAlchemy: the registry's forest
projected to `g_nodes`, the latest layout per scada, a rolling window of
readings, and the alerter's alert state.

Every row in and out is a typed record: sema instances through the codec,
or the interior records below. Which houses are tracked is not a table:
it is every Active TerminalAsset in `g_nodes` under the fleet roots. The
`HouseRecord` here is the interim for a sema liveness record word (one per
scada: last heard, mode, live flag); when that word is published the store
holds instances of it and this record goes.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import NamedTuple

from alembic import command
from alembic.config import Config
from pydantic import TypeAdapter
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from gwalerter.db_models import AlertSql, GNodeSql, LayoutSql, ReadingSql
from gwalerter.sema.codec import default_codec
from gwalerter.sema.enums import GNodeStatus, HouseAlertKind
from gwalerter.sema.property_format import (
    LeftRightDot,
    SpaceheatName,
    UTCMilliseconds,
)
from gwalerter.sema.types import (
    GNodeForest,
    GNodeGt,
    HouseAlert,
    HouseAlertCleared,
    LayoutLite,
    ReportEvent,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
TERMINAL_ASSET_CLASS = "TerminalAsset"
TERMINAL_ASSET_SUFFIX = ".ta"

LRD = TypeAdapter(LeftRightDot)
SH_NAME = TypeAdapter(SpaceheatName)
UTC_MS = TypeAdapter(UTCMilliseconds)


class Reading(NamedTuple):
    """One reading of one channel: the raw integer value the scada sent
    (its unit and exponent come from the house layout) and when the scada
    read it."""

    channel: SpaceheatName
    value: int
    read_ms: UTCMilliseconds


class HouseRecord(NamedTuple):
    """What the store knows about one scada: when it was last heard from
    (any tracked message arriving), and when its latest layout arrived.
    `None` where the scada has never sent that kind of message."""

    alias: LeftRightDot
    last_heard_ms: UTCMilliseconds | None
    layout_received_ms: UTCMilliseconds | None


def migrate(url: str) -> None:
    """Bring the database at `url` to the head of the migration chain."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


def parent_alias(alias: LeftRightDot) -> str:
    return alias.rsplit(".", 1)[0]


class Store:
    """sqlite behind a lock: the actor's consumer thread writes, detectors
    read. Open with `Store.open`, which applies the migration chain;
    `close` when the actor stops."""

    def __init__(
        self,
        engine: Engine,
        *,
        readings_window_s: int,
        fleet_roots: list[LeftRightDot],
    ) -> None:
        self.engine = engine
        self.readings_window_s = readings_window_s
        self.fleet_roots = fleet_roots
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.lock = threading.Lock()

    @classmethod
    def open(
        cls, url: str, *, readings_window_s: int, fleet_roots: list[LeftRightDot]
    ) -> Store:
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(
                parents=True, exist_ok=True
            )
        migrate(url)
        engine = create_engine(url, connect_args={"check_same_thread": False})
        return cls(engine, readings_window_s=readings_window_s, fleet_roots=fleet_roots)

    def close(self) -> None:
        with self.lock:
            self.engine.dispose()

    def session(self) -> Session:
        return self.sessions()

    # -- registry projection --------------------------------------------

    def upsert_forest(self, forest: GNodeForest) -> None:
        """Upsert every node by its immutable id; a forest older than the
        one that last wrote a row leaves that row alone."""
        with self.lock, self.session() as s:
            for node in forest.nodes:
                row = s.get(GNodeSql, node.g_node_id)
                if (
                    row is not None
                    and row.send_time_ms is not None
                    and forest.send_time_ms is not None
                    and forest.send_time_ms < row.send_time_ms
                ):
                    continue
                if row is None:
                    row = GNodeSql(g_node_id=node.g_node_id)
                    s.add(row)
                row.alias = node.alias
                row.g_node_class = node.g_node_class
                row.status = node.status.value
                row.send_time_ms = forest.send_time_ms
                row.payload = node.to_dict()
            s.commit()

    def g_node(self, alias: LeftRightDot) -> GNodeGt | None:
        with self.lock, self.session() as s:
            row = s.scalar(select(GNodeSql).where(GNodeSql.alias == alias))
        return None if row is None else self.g_node_of(row)

    def tracked_houses(self) -> list[GNodeGt]:
        """Every Active TerminalAsset under the fleet roots, by alias."""
        with self.lock, self.session() as s:
            rows = s.scalars(
                select(GNodeSql)
                .where(GNodeSql.g_node_class == TERMINAL_ASSET_CLASS)
                .where(GNodeSql.status == GNodeStatus.Active.value)
                .order_by(GNodeSql.alias)
            ).all()
        return [self.g_node_of(r) for r in rows if self.under_roots(r.alias)]

    def tracked_house_of(self, scada_alias: LeftRightDot) -> GNodeGt | None:
        """The tracked terminal asset a scada reports for: the sibling
        `.ta` under the scada's parent alias, if it is tracked."""
        house = self.g_node(parent_alias(scada_alias) + TERMINAL_ASSET_SUFFIX)
        if (
            house is None
            or house.g_node_class != TERMINAL_ASSET_CLASS
            or house.status != GNodeStatus.Active
            or not self.under_roots(house.alias)
        ):
            return None
        return house

    def under_roots(self, alias: str) -> bool:
        return any(
            alias == root or alias.startswith(root + ".") for root in self.fleet_roots
        )

    def g_node_of(self, row: GNodeSql) -> GNodeGt:
        return default_codec.from_dict(row.payload, expect=GNodeGt)

    # -- writes ----------------------------------------------------------

    def record_layout(
        self, layout: LayoutLite, *, received_ms: UTCMilliseconds
    ) -> None:
        """Keep the latest layout per scada; its arrival is a sign of life."""
        with self.lock, self.session() as s:
            row = s.get(LayoutSql, layout.from_g_node_alias)
            if row is None:
                row = LayoutSql(scada_alias=layout.from_g_node_alias)
                s.add(row)
            row.type_name = layout.type_name
            row.version = layout.version
            row.received_ms = received_ms
            row.payload = layout.to_dict()
            s.commit()

    def record_report(
        self, event: ReportEvent, *, received_ms: UTCMilliseconds
    ) -> None:
        """Append every reading in the report; readings that arrived before
        the window leave in the same transaction."""
        rows = [
            ReadingSql(
                scada_alias=event.src,
                channel=readings.channel_name,
                value=value,
                read_ms=read_ms,
                received_ms=received_ms,
            )
            for readings in event.report.channel_reading_list
            for value, read_ms in zip(
                readings.value_list, readings.scada_read_time_unix_ms_list, strict=True
            )
        ]
        with self.lock, self.session() as s:
            s.add_all(rows)
            s.execute(
                delete(ReadingSql).where(
                    ReadingSql.received_ms < received_ms - self.readings_window_s * 1000
                )
            )
            s.commit()

    # -- reads -----------------------------------------------------------

    def houses(self) -> list[HouseRecord]:
        """Every scada heard from, with last-heard as a query over readings
        and layouts: the latest arrival of either."""
        with self.lock, self.session() as s:
            heard: dict[str, int] = {
                alias: ms
                for alias, ms in s.execute(
                    select(
                        ReadingSql.scada_alias, func.max(ReadingSql.received_ms)
                    ).group_by(ReadingSql.scada_alias)
                )
            }
            layouts: dict[str, int] = {
                alias: ms
                for alias, ms in s.execute(
                    select(LayoutSql.scada_alias, LayoutSql.received_ms)
                )
            }
        records = []
        for alias in sorted(set(heard) | set(layouts)):
            got = layouts.get(alias)
            last = max(v for v in (heard.get(alias), got) if v is not None)
            records.append(
                HouseRecord(
                    alias=LRD.validate_python(alias),
                    last_heard_ms=UTC_MS.validate_python(last),
                    layout_received_ms=None
                    if got is None
                    else UTC_MS.validate_python(got),
                )
            )
        return records

    def layout(self, scada_alias: LeftRightDot) -> LayoutLite | None:
        with self.lock, self.session() as s:
            row = s.get(LayoutSql, scada_alias)
        if row is None:
            return None
        return default_codec.from_dict(row.payload, expect=LayoutLite)

    def latest_reading(
        self, scada_alias: LeftRightDot, channel: SpaceheatName
    ) -> Reading | None:
        with self.lock, self.session() as s:
            row = s.scalar(
                select(ReadingSql)
                .where(
                    ReadingSql.scada_alias == scada_alias, ReadingSql.channel == channel
                )
                .order_by(ReadingSql.read_ms.desc())
                .limit(1)
            )
        return None if row is None else self.reading(row)

    def readings_since(
        self,
        scada_alias: LeftRightDot,
        channel: SpaceheatName,
        since_ms: UTCMilliseconds,
    ) -> list[Reading]:
        with self.lock, self.session() as s:
            rows = s.scalars(
                select(ReadingSql)
                .where(
                    ReadingSql.scada_alias == scada_alias,
                    ReadingSql.channel == channel,
                    ReadingSql.read_ms >= since_ms,
                )
                .order_by(ReadingSql.read_ms)
            ).all()
        return [self.reading(row) for row in rows]

    # -- alert state ------------------------------------------------------

    def open_alert(
        self, about_g_node_alias: LeftRightDot, kind: HouseAlertKind
    ) -> HouseAlert | None:
        """The alert of this kind currently open on this house, if any: the
        row with no cleared word. At most one is open per house and kind."""
        with self.lock, self.session() as s:
            row = s.scalar(
                select(AlertSql)
                .where(AlertSql.about_g_node_alias == about_g_node_alias)
                .where(AlertSql.kind == kind.value)
                .where(AlertSql.cleared_ms.is_(None))
                .order_by(AlertSql.raised_ms.desc())
            )
        if row is None:
            return None
        return default_codec.from_dict(row.payload, expect=HouseAlert)

    def raise_alert(self, alert: HouseAlert) -> None:
        """Record a raised alert word. Recorded before it is broadcast, so a
        restart between the two neither re-raises nor forgets it."""
        with self.lock, self.session() as s:
            s.add(
                AlertSql(
                    alert_id=alert.alert_id,
                    about_g_node_alias=alert.about_g_node_alias,
                    kind=alert.kind.value,
                    raised_ms=alert.raised_ms,
                    payload=alert.to_dict(),
                )
            )
            s.commit()

    def clear_alert(self, cleared: HouseAlertCleared) -> None:
        """Record the cleared word against its alert, which closes it."""
        with self.lock, self.session() as s:
            row = s.get(AlertSql, cleared.alert_id)
            if row is None:
                raise KeyError(f"no alert {cleared.alert_id} to clear")
            row.cleared_ms = cleared.cleared_ms
            row.cleared_payload = cleared.to_dict()
            s.commit()

    def alerts(self, about_g_node_alias: LeftRightDot) -> list[HouseAlert]:
        """Every alert ever raised on a house, oldest first."""
        with self.lock, self.session() as s:
            rows = s.scalars(
                select(AlertSql)
                .where(AlertSql.about_g_node_alias == about_g_node_alias)
                .order_by(AlertSql.raised_ms)
            ).all()
        return [default_codec.from_dict(r.payload, expect=HouseAlert) for r in rows]

    def reading(self, row: ReadingSql) -> Reading:
        return Reading(
            channel=SH_NAME.validate_python(row.channel),
            value=row.value,
            read_ms=UTC_MS.validate_python(row.read_ms),
        )
