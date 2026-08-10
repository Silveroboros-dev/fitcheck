"""step 6 — odds capture + actor-scoped odds locks

Closes the loop to a saved ledger entry (Phase-1 exit). Three schema moves:

- candidate_set_members.current_probability: the FROZEN market YES probability
  from the retrieval snapshot, captured at retrieval. ledger_entries.
  odds_at_entry is copied (thesis-side oriented) from here, never re-fetched.
- ledger_entries.odds_at_entry_side: yes | no | side_unknown — the side the
  odds were oriented to (P1 thesis_side), so odds_at_entry is self-describing.
- odds_locks: bound to AUTHENTICATED ACTOR IDENTITY, not only the spoofable
  client_ref (review amendment). Adds client_type + actor_id (stringified
  user_id / api_client_id / agent_client_id) + conviction_event_id (the blind
  event the lock unlocked; reveal stamps its odds_revealed_at, write-once).
  The uniqueness key becomes (thesis_analysis_id, client_type, actor_id,
  client_ref). odds_locks is empty in every environment, so the NOT NULL adds
  and the constraint swap are safe.

Revision ID: b1f7c0d9e2a3
Revises: 3496a0e487e3
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1f7c0d9e2a3"
down_revision: Union[str, Sequence[str], None] = "3496a0e487e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("candidate_set_members") as batch:
        batch.add_column(
            sa.Column("current_probability", sa.Float(), nullable=True)
        )
    with op.batch_alter_table("ledger_entries") as batch:
        batch.add_column(
            sa.Column("odds_at_entry_side", sa.String(length=16), nullable=True)
        )
    with op.batch_alter_table("odds_locks", recreate="always") as batch:
        batch.add_column(
            sa.Column("client_type", sa.String(length=16), nullable=False)
        )
        batch.add_column(
            sa.Column("actor_id", sa.String(length=160), nullable=False)
        )
        batch.add_column(
            sa.Column("conviction_event_id", sa.Uuid(), nullable=True)
        )
        batch.drop_constraint("uq_odds_lock", type_="unique")
        batch.create_unique_constraint(
            "uq_odds_lock",
            ["thesis_analysis_id", "client_type", "actor_id", "client_ref"],
        )


def downgrade() -> None:
    with op.batch_alter_table("odds_locks", recreate="always") as batch:
        batch.drop_constraint("uq_odds_lock", type_="unique")
        batch.drop_column("conviction_event_id")
        batch.drop_column("actor_id")
        batch.drop_column("client_type")
        batch.create_unique_constraint(
            "uq_odds_lock", ["thesis_analysis_id", "client_ref"]
        )
    with op.batch_alter_table("ledger_entries") as batch:
        batch.drop_column("odds_at_entry_side")
    with op.batch_alter_table("candidate_set_members") as batch:
        batch.drop_column("current_probability")
