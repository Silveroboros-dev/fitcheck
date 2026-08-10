"""draft contract echo fields

Step 5b follow-up (external review): surface the gate-verified echo fields
as first-class columns instead of leaving them only inside the provenance
blob — resolution_deadline, resolution_source_class, subject_entity are
the draft's audit substance. Nullable additive columns (the service always
populates them; draft_contracts is empty in every environment).

Revision ID: 3496a0e487e3
Revises: ee90f333bae6
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3496a0e487e3"
down_revision: Union[str, Sequence[str], None] = "ee90f333bae6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("draft_contracts") as batch:
        batch.add_column(
            sa.Column("resolution_deadline", sa.Date(), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "resolution_source_class", sa.String(length=16), nullable=True
            )
        )
        batch.add_column(
            sa.Column("subject_entity", sa.String(length=256), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("draft_contracts") as batch:
        batch.drop_column("subject_entity")
        batch.drop_column("resolution_source_class")
        batch.drop_column("resolution_deadline")
