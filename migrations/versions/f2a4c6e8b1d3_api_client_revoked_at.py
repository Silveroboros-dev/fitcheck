"""api_clients.revoked_at — key revocation/expiry

resolve_principal authenticated any key that ever existed; there was no way to
disable one short of deleting the row. Adds a nullable revoked_at timestamp — a
non-NULL value disables the key, and resolve_principal rejects it regardless of
client_type. Additive and nullable, so existing keys stay live (revoked_at NULL).

Revision ID: f2a4c6e8b1d3
Revises: d5e7f1a9c3b4
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a4c6e8b1d3"
down_revision: Union[str, Sequence[str], None] = "d5e7f1a9c3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("api_clients") as batch:
        batch.add_column(
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("api_clients") as batch:
        batch.drop_column("revoked_at")
