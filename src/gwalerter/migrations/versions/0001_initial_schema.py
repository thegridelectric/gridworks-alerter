"""initial schema: g_nodes, layouts, readings, alerts

Revision ID: 0001
Revises:
Create Date: 2026-09-15 20:20:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "g_nodes",
        sa.Column("g_node_id", sa.String(), nullable=False),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("g_node_class", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("send_time_ms", sa.BigInteger(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("g_node_id"),
    )
    op.create_index(op.f("ix_g_nodes_alias"), "g_nodes", ["alias"], unique=True)
    op.create_table(
        "layouts",
        sa.Column("scada_alias", sa.String(), nullable=False),
        sa.Column("type_name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("received_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("scada_alias"),
    )
    op.create_table(
        "readings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scada_alias", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("read_ms", sa.BigInteger(), nullable=False),
        sa.Column("received_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_readings_scada_channel_read",
        "readings",
        ["scada_alias", "channel", "read_ms"],
        unique=False,
    )
    op.create_index(
        op.f("ix_readings_received_ms"), "readings", ["received_ms"], unique=False
    )
    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.String(), nullable=False),
        sa.Column("about_g_node_alias", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("raised_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("cleared_ms", sa.BigInteger(), nullable=True),
        sa.Column("cleared_payload", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("alert_id"),
    )
    op.create_index(
        "ix_alerts_about_kind_cleared",
        "alerts",
        ["about_g_node_alias", "kind", "cleared_ms"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_alerts_about_kind_cleared", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index(op.f("ix_readings_received_ms"), table_name="readings")
    op.drop_index("ix_readings_scada_channel_read", table_name="readings")
    op.drop_table("readings")
    op.drop_table("layouts")
    op.drop_index(op.f("ix_g_nodes_alias"), table_name="g_nodes")
    op.drop_table("g_nodes")
