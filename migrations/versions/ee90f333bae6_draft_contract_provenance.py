"""draft contract provenance

Step 5b: every persisted draft carries complete run/policy provenance
(blueprint §5 — partial provenance is no provenance), mirroring fit_cards
and market_recommendations. The draft is a separate proposer call with
its own model_run_id, so its provenance cannot be folded into the card's.

draft_contracts is empty in every environment (draft generation ships in
this step), so the NOT NULL add carries a transient server_default that
production never exercises — the service always writes a complete blob.

Revision ID: ee90f333bae6
Revises: 9b4e6a2d51c8
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ee90f333bae6"
down_revision: Union[str, Sequence[str], None] = "9b4e6a2d51c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROVENANCE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    with op.batch_alter_table("draft_contracts") as batch:
        batch.add_column(
            sa.Column(
                "provenance",
                _PROVENANCE,
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("draft_contracts") as batch:
        batch.drop_column("provenance")
