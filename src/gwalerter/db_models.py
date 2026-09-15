"""SQLAlchemy models for the alerter's sqlite store: one table per sema
word the alerter keeps, nothing invented.

Payload columns hold the word's wire form (PascalCase, via `to_dict`) and
rows load back as codec-validated instances in `store.py`, never as raw
tuples; the other columns are what queries filter and sort on. The schema
is applied by the alembic chain under `migrations/`, never `create_all`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class GNodeSql(Base):
    """One `g.node.gt` from the registry's forest, upserted by its immutable
    id. `send_time_ms` is the forest's SendTimeMs that last wrote the row,
    so an older forest never overwrites a newer one."""

    __tablename__ = "g_nodes"

    g_node_id: Mapped[str] = mapped_column(String, primary_key=True)
    alias: Mapped[str] = mapped_column(String, unique=True, index=True)
    g_node_class: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    send_time_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class LayoutSql(Base):
    """The latest hardware layout word a scada sent, whichever word that is
    (`layout.lite` today), by type name and version."""

    __tablename__ = "layouts"

    scada_alias: Mapped[str] = mapped_column(String, primary_key=True)
    type_name: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    received_ms: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ReadingSql(Base):
    """`channel.readings` unrolled: one row per value, with the scada's
    read time and the alerter's arrival time. Rows older than the window
    leave on every report."""

    __tablename__ = "readings"
    __table_args__ = (
        Index("ix_readings_scada_channel_read", "scada_alias", "channel", "read_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scada_alias: Mapped[str] = mapped_column(String)
    channel: Mapped[str] = mapped_column(String)
    value: Mapped[int] = mapped_column(BigInteger)
    read_ms: Mapped[int] = mapped_column(BigInteger)
    received_ms: Mapped[int] = mapped_column(BigInteger, index=True)


class AlertSql(Base):
    """One `gw.house.alert` and, once it ends, its `gw.house.alert.cleared`:
    the alerter's open-alert state, which survives a restart."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index(
            "ix_alerts_about_kind_cleared", "about_g_node_alias", "kind", "cleared_ms"
        ),
    )

    alert_id: Mapped[str] = mapped_column(String, primary_key=True)
    about_g_node_alias: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    raised_ms: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    cleared_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cleared_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
