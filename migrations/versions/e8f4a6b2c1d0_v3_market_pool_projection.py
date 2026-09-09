"""v3 per-market assessments, display projection, and human choice

Revision ID: e8f4a6b2c1d0
Revises: c6b5e4d3a2f1
Create Date: 2026-09-04

Additive only. Legacy fit cards, recommendations, rejections, and ledger rows
retain their historical semantics.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8f4a6b2c1d0"
down_revision: Union[str, Sequence[str], None] = "c6b5e4d3a2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "market_assessments",
        sa.Column("fit_card_id", sa.Uuid(), nullable=False),
        sa.Column("thesis_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_set_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_set_member_id", sa.Uuid(), nullable=False),
        sa.Column("rules_capture_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("market_id", sa.String(length=128), nullable=False),
        sa.Column("retrieval_rank", sa.Integer(), nullable=False),
        sa.Column("pair_class", sa.String(length=32), nullable=False),
        sa.Column("what_it_captures", sa.Text(), nullable=False),
        sa.Column("what_it_misses", sa.Text(), nullable=False),
        sa.Column("horizon_match", sa.String(length=8), nullable=True),
        sa.Column("resolution_risk", sa.String(length=8), nullable=True),
        sa.Column("authority", sa.String(length=64), nullable=False),
        sa.Column("fit_confidence", sa.Float(), nullable=True),
        sa.Column("provenance", JSON_VARIANT, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "retrieval_rank > 0", name="ck_market_assessment_retrieval_rank"
        ),
        sa.CheckConstraint(
            "pair_class IN ('direct', 'indirect', 'weak_proxy', "
            "'not_an_expression')",
            name="ck_market_assessment_pair_class",
        ),
        sa.ForeignKeyConstraint(["fit_card_id"], ["fit_cards.id"]),
        sa.ForeignKeyConstraint(
            ["thesis_analysis_id"], ["thesis_analyses.id"]
        ),
        sa.ForeignKeyConstraint(["candidate_set_id"], ["candidate_sets.id"]),
        sa.ForeignKeyConstraint(
            ["candidate_set_member_id"], ["candidate_set_members.id"]
        ),
        sa.ForeignKeyConstraint(
            ["rules_capture_id"], ["market_rules_captures.id"]
        ),
        sa.ForeignKeyConstraint(["snapshot_id"], ["market_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fit_card_id", "market_id", name="uq_market_assessment_pair"
        ),
    )

    op.create_table(
        "market_display_sets",
        sa.Column("fit_card_id", sa.Uuid(), nullable=False),
        sa.Column("thesis_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_set_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("display_policy_version", sa.String(length=64), nullable=False),
        sa.Column("assessed_count", sa.Integer(), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("displayed_count", sa.Integer(), nullable=False),
        sa.Column("assessment_complete", sa.Boolean(), nullable=False),
        sa.Column("system_pool_outcome", sa.String(length=32), nullable=False),
        sa.Column("incomplete_reasons", JSON_VARIANT, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "assessed_count >= 0", name="ck_market_display_assessed_count"
        ),
        sa.CheckConstraint(
            "target_count >= 0 AND target_count <= 3",
            name="ck_market_display_target_count",
        ),
        sa.CheckConstraint(
            "displayed_count >= 0 AND displayed_count <= 3",
            name="ck_market_display_displayed_count",
        ),
        sa.CheckConstraint(
            "displayed_count <= assessed_count",
            name="ck_market_display_count_within_assessed",
        ),
        sa.CheckConstraint(
            "system_pool_outcome IN ('candidate_expressions', "
            "'no_clean_expression', 'incomplete')",
            name="ck_market_display_pool_outcome",
        ),
        sa.ForeignKeyConstraint(["fit_card_id"], ["fit_cards.id"]),
        sa.ForeignKeyConstraint(
            ["thesis_analysis_id"], ["thesis_analyses.id"]
        ),
        sa.ForeignKeyConstraint(["candidate_set_id"], ["candidate_sets.id"]),
        sa.ForeignKeyConstraint(["snapshot_id"], ["market_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fit_card_id"),
    )

    op.create_table(
        "market_display_items",
        sa.Column("market_display_set_id", sa.Uuid(), nullable=False),
        sa.Column("market_assessment_id", sa.Uuid(), nullable=False),
        sa.Column("display_rank", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "display_rank >= 1 AND display_rank <= 3",
            name="ck_market_display_rank",
        ),
        sa.ForeignKeyConstraint(
            ["market_display_set_id"], ["market_display_sets.id"]
        ),
        sa.ForeignKeyConstraint(
            ["market_assessment_id"], ["market_assessments.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_assessment_id"),
        sa.UniqueConstraint(
            "market_display_set_id",
            "display_rank",
            name="uq_market_display_rank",
        ),
    )

    op.create_table(
        "market_choices",
        sa.Column("market_display_set_id", sa.Uuid(), nullable=False),
        sa.Column("selection_kind", sa.String(length=16), nullable=False),
        sa.Column("market_assessment_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.String(length=160), nullable=False),
        sa.Column("client_type", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "selection_kind IN ('market', 'none')",
            name="ck_market_choice_selection_kind",
        ),
        sa.CheckConstraint(
            "((selection_kind = 'market' AND market_assessment_id IS NOT NULL) "
            "OR (selection_kind = 'none' AND market_assessment_id IS NULL))",
            name="ck_market_choice_selection_shape",
        ),
        sa.ForeignKeyConstraint(
            ["market_display_set_id"], ["market_display_sets.id"]
        ),
        sa.ForeignKeyConstraint(
            ["market_assessment_id"], ["market_assessments.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_display_set_id"),
    )


def downgrade() -> None:
    op.drop_table("market_choices")
    op.drop_table("market_display_items")
    op.drop_table("market_display_sets")
    op.drop_table("market_assessments")
