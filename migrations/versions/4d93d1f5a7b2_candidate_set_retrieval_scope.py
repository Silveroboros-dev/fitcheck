"""persist a bounded retrieval-scope projection with candidate sets

Revision ID: 4d93d1f5a7b2
Revises: ab73d8e4f901
Create Date: 2026-09-13

The nullable column intentionally leaves existing candidate sets unknown.  A
new runtime must not reconstruct a historical query from current provider
configuration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "4d93d1f5a7b2"
down_revision: Union[str, Sequence[str], None] = "ab73d8e4f901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    with op.batch_alter_table("candidate_sets") as batch_op:
        batch_op.add_column(
            sa.Column("retrieval_scope", JSON_VARIANT, nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("candidate_sets") as batch_op:
        batch_op.drop_column("retrieval_scope")
