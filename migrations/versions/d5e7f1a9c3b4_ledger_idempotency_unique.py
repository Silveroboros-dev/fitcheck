"""ledger idempotency — unique (thesis_analysis_id, user_id)

create_ledger_entry is idempotent per (user, thesis): a second save returns the
first. Until now that was enforced only by a check-then-insert in the service,
which races — two concurrent or retried saves both pass the existence check and
write two rows. This adds the DB constraint that actually backs the guarantee;
the service catches the IntegrityError and returns the existing entry.

ledger_entries is referenced by conviction_events.ledger_entry_id, so we let
alembic pick the strategy (ALTER on Postgres, table-copy only where SQLite
requires it) rather than forcing a recreate.

Revision ID: d5e7f1a9c3b4
Revises: c3a9f1e5d7b2
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d5e7f1a9c3b4"
down_revision: Union[str, Sequence[str], None] = "c3a9f1e5d7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("ledger_entries") as batch:
        batch.create_unique_constraint(
            "uq_ledger_per_user_thesis", ["thesis_analysis_id", "user_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("ledger_entries") as batch:
        batch.drop_constraint("uq_ledger_per_user_thesis", type_="unique")
