"""bounded MCP usage buckets

Revision ID: e6c7a8b9d0f1
Revises: a4d8c2e6f0b1
Create Date: 2026-08-11

Each API client owns one row per stable quota scope. A row is overwritten when
its fixed window rolls, keeping storage bounded independently of call volume.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6c7a8b9d0f1"
down_revision: Union[str, Sequence[str], None] = "a4d8c2e6f0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_usage_buckets",
        sa.Column("api_client_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column(
            "window_started_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("used_units", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "used_units >= 0", name="ck_mcp_usage_units_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["api_client_id"], ["api_clients.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("api_client_id", "scope"),
    )


def downgrade() -> None:
    op.drop_table("mcp_usage_buckets")
