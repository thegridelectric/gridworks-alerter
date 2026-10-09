"""arrivals: latest envelope arrival per alias

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 18:30:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "arrivals",
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("received_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("alias"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("arrivals")
