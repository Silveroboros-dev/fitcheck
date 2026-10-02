"""add a separate accepted W3C trace context to durable jobs

Revision ID: 6a01d9c2b7e4
Revises: 4d93d1f5a7b2
Create Date: 2026-09-16

This nullable operational field is intentionally excluded from job payloads,
pinned manifests, semantic hashes, idempotency, and business correlations.
It contains only a validated W3C traceparent, never source/provider content.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "6a01d9c2b7e4"
down_revision: Union[str, Sequence[str], None] = "4d93d1f5a7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column("accepted_traceparent", sa.String(length=55), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("accepted_traceparent")
