"""silences: the houses the tap does not page

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15 16:00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "silences",
        sa.Column("alias", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("alias"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("silences")
