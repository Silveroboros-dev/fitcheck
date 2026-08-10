"""odds lock actor-level unique — drop client_ref from the lock key

Review blocker: with client_ref in the uniqueness key, one authenticated actor
could submit a blind prior, see odds, then submit again under a NEW client_ref
to mint a fresh lock and overwrite the (now post-reveal) prior — bypassing the
blind-prior freeze. The lock identity is ACTOR-LEVEL: thesis + client_type +
actor_id. client_ref stays as a session/idempotency metadata column, never a
security/protocol key. odds_locks is empty in every environment.

Revision ID: c3a9f1e5d7b2
Revises: b1f7c0d9e2a3
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c3a9f1e5d7b2"
down_revision: Union[str, Sequence[str], None] = "b1f7c0d9e2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("odds_locks", recreate="always") as batch:
        batch.drop_constraint("uq_odds_lock", type_="unique")
        batch.create_unique_constraint(
            "uq_odds_lock", ["thesis_analysis_id", "client_type", "actor_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("odds_locks", recreate="always") as batch:
        batch.drop_constraint("uq_odds_lock", type_="unique")
        batch.create_unique_constraint(
            "uq_odds_lock",
            ["thesis_analysis_id", "client_type", "actor_id", "client_ref"],
        )
