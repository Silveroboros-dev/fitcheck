"""fit confidence nullable

Ratification item 9 amendment (external review): a deterministic-fallback
fit card has no calibrated confidence. NULL + provenance.confidence_source,
never a 0.5 sentinel. Applies to fit_cards.fit_confidence and its mirror
market_recommendations.fit_score.

Revision ID: 9b4e6a2d51c8
Revises: 7c2e91ab4d10
Create Date: 2026-06-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9b4e6a2d51c8"
down_revision: Union[str, Sequence[str], None] = "7c2e91ab4d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("fit_cards") as batch:
        batch.alter_column(
            "fit_confidence", existing_type=sa.Float(), nullable=True
        )
    with op.batch_alter_table("market_recommendations") as batch:
        batch.alter_column(
            "fit_score", existing_type=sa.Float(), nullable=True
        )


def downgrade() -> None:
    with op.batch_alter_table("market_recommendations") as batch:
        batch.alter_column(
            "fit_score", existing_type=sa.Float(), nullable=False
        )
    with op.batch_alter_table("fit_cards") as batch:
        batch.alter_column(
            "fit_confidence", existing_type=sa.Float(), nullable=False
        )
