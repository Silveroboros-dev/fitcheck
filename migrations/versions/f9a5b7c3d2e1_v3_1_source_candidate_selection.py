"""v3.1 source interpretation, human candidate choice, and lineage

Revision ID: f9a5b7c3d2e1
Revises: e8f4a6b2c1d0
Create Date: 2026-09-05

Additive only. Existing normalization attempts remain valid with null source
candidate lineage; no governed review or golden data is rewritten.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f9a5b7c3d2e1"
down_revision: Union[str, Sequence[str], None] = "e8f4a6b2c1d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "source_interpretations",
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reasons", JSON_VARIANT, nullable=False),
        sa.Column("prompt_policy_version", sa.String(length=64), nullable=False),
        sa.Column("system_variant_id", sa.String(length=192), nullable=False),
        sa.Column("model_adapter", sa.String(length=128), nullable=True),
        sa.Column("model_run_id", sa.String(length=128), nullable=True),
        sa.Column("client_type", sa.String(length=16), nullable=False),
        sa.Column("agent_client_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('candidates', 'refusal')",
            name="ck_source_interpretation_outcome",
        ),
        sa.CheckConstraint(
            "((input_text IS NULL AND input_digest IS NULL) OR "
            "(input_text IS NOT NULL AND input_digest IS NOT NULL))",
            name="ck_source_interpretation_input_retention",
        ),
        sa.CheckConstraint(
            "((model_adapter IS NULL AND model_run_id IS NULL) OR "
            "(model_adapter IS NOT NULL AND model_run_id IS NOT NULL))",
            name="ck_source_interpretation_model_provenance",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "source_thesis_candidates",
        sa.Column("source_interpretation_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("selected_source_quote", sa.Text(), nullable=False),
        sa.Column("source_quote_digest", sa.String(length=64), nullable=False),
        sa.Column("claim_summary", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1 AND ordinal <= 3",
            name="ck_source_thesis_candidate_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["source_interpretation_id"], ["source_interpretations.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_interpretation_id",
            "ordinal",
            name="uq_source_thesis_candidate_ordinal",
        ),
        sa.UniqueConstraint(
            "source_interpretation_id",
            "source_quote_digest",
            name="uq_source_thesis_candidate_quote",
        ),
    )
    op.create_table(
        "source_candidate_choices",
        sa.Column("source_interpretation_id", sa.Uuid(), nullable=False),
        sa.Column("selection_kind", sa.String(length=16), nullable=False),
        sa.Column("source_thesis_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.String(length=160), nullable=False),
        sa.Column("client_type", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "selection_kind IN ('candidate', 'none')",
            name="ck_source_candidate_choice_selection_kind",
        ),
        sa.CheckConstraint(
            "((selection_kind = 'candidate' AND "
            "source_thesis_candidate_id IS NOT NULL) OR "
            "(selection_kind = 'none' AND "
            "source_thesis_candidate_id IS NULL))",
            name="ck_source_candidate_choice_selection_shape",
        ),
        sa.ForeignKeyConstraint(
            ["source_interpretation_id"], ["source_interpretations.id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_thesis_candidate_id"], ["source_thesis_candidates.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_interpretation_id",
            name="uq_source_candidate_choice_interpretation",
        ),
    )
    with op.batch_alter_table("normalization_attempts") as batch_op:
        batch_op.add_column(
            sa.Column("source_interpretation_id", sa.Uuid(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("source_thesis_candidate_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_normalization_attempt_source_interpretation",
            "source_interpretations",
            ["source_interpretation_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_normalization_attempt_source_candidate",
            "source_thesis_candidates",
            ["source_thesis_candidate_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "uq_normalization_attempt_source_candidate",
            ["source_thesis_candidate_id"],
        )
        batch_op.create_check_constraint(
            "ck_normalization_attempt_source_binding",
            "((source_interpretation_id IS NULL AND "
            "source_thesis_candidate_id IS NULL) OR "
            "(source_interpretation_id IS NOT NULL AND "
            "source_thesis_candidate_id IS NOT NULL))",
        )


def downgrade() -> None:
    with op.batch_alter_table("normalization_attempts") as batch_op:
        batch_op.drop_constraint(
            "ck_normalization_attempt_source_binding", type_="check"
        )
        batch_op.drop_constraint(
            "uq_normalization_attempt_source_candidate", type_="unique"
        )
        batch_op.drop_constraint(
            "fk_normalization_attempt_source_candidate", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_normalization_attempt_source_interpretation", type_="foreignkey"
        )
        batch_op.drop_column("source_thesis_candidate_id")
        batch_op.drop_column("source_interpretation_id")
    op.drop_table("source_candidate_choices")
    op.drop_table("source_thesis_candidates")
    op.drop_table("source_interpretations")
