"""normalization confirmation evidence and human decisions

Revision ID: c6b5e4d3a2f1
Revises: a4d8c2e6f0b1
Create Date: 2026-09-04

Additive only: legacy thesis analyses and product routes keep their current
shape. New v3 writes enter through immutable attempts and terminal decisions.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6b5e4d3a2f1"
down_revision: Union[str, Sequence[str], None] = "e6c7a8b9d0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "normalization_attempts",
        sa.Column("predecessor_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("proposal", JSON_VARIANT, nullable=True),
        sa.Column("clarifying_question", sa.String(length=300), nullable=True),
        sa.Column("reasons", JSON_VARIANT, nullable=False),
        sa.Column("gate_policy_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_policy_version", sa.String(length=64), nullable=False),
        sa.Column("system_variant_id", sa.String(length=192), nullable=False),
        sa.Column("model_adapter", sa.String(length=128), nullable=True),
        sa.Column("model_run_id", sa.String(length=128), nullable=True),
        sa.Column("client_type", sa.String(length=16), nullable=False),
        sa.Column("agent_client_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('candidate', 'clarification', 'refusal')",
            name="ck_normalization_attempt_outcome",
        ),
        sa.CheckConstraint(
            "((input_text IS NULL AND input_digest IS NULL) OR "
            "(input_text IS NOT NULL AND input_digest IS NOT NULL))",
            name="ck_normalization_attempt_input_retention",
        ),
        sa.CheckConstraint(
            "((model_adapter IS NULL AND model_run_id IS NULL) OR "
            "(model_adapter IS NOT NULL AND model_run_id IS NOT NULL))",
            name="ck_normalization_attempt_model_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_attempt_id"], ["normalization_attempts.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "predecessor_attempt_id",
            name="uq_normalization_attempt_predecessor",
        ),
    )

    op.create_table(
        "normalization_decisions",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("thesis_analysis_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.String(length=160), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('accept', 'edit', 'reject')",
            name="ck_normalization_decision_action",
        ),
        sa.CheckConstraint(
            "((action = 'accept' AND thesis_analysis_id IS NOT NULL) OR "
            "(action <> 'accept' AND thesis_analysis_id IS NULL))",
            name="ck_normalization_decision_analysis_shape",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["normalization_attempts.id"]
        ),
        sa.ForeignKeyConstraint(
            ["thesis_analysis_id"], ["thesis_analyses.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint("thesis_analysis_id"),
    )


def downgrade() -> None:
    op.drop_table("normalization_decisions")
    op.drop_table("normalization_attempts")
