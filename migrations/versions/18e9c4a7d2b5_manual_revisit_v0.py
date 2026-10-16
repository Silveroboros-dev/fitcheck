"""Add bounded manual revisit records.

Revision ID: 18e9c4a7d2b5
Revises: 6a01d9c2b7e4
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "18e9c4a7d2b5"
down_revision: Union[str, Sequence[str], None] = "6a01d9c2b7e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _id():
    return sa.Column("id", sa.Uuid(), primary_key=True)


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "personal_theses", _id(), _created(),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("active_revision_id", sa.Uuid(), nullable=True),
    )
    op.create_table(
        "thesis_revisions", _id(), _created(),
        sa.Column("personal_thesis_id", sa.Uuid(), sa.ForeignKey("personal_theses.id"), nullable=False),
        sa.Column("thesis_analysis_id", sa.Uuid(), sa.ForeignKey("thesis_analyses.id"), nullable=False, unique=True),
        sa.Column("normalization_decision_id", sa.Uuid(), sa.ForeignKey("normalization_decisions.id"), nullable=False),
        sa.Column("source_candidate_choice_id", sa.Uuid(), sa.ForeignKey("source_candidate_choices.id"), nullable=True),
        sa.Column("market_display_set_id", sa.Uuid(), sa.ForeignKey("market_display_sets.id"), nullable=False),
        sa.Column("market_choice_id", sa.Uuid(), sa.ForeignKey("market_choices.id"), nullable=False, unique=True),
        sa.Column("market_assessment_id", sa.Uuid(), sa.ForeignKey("market_assessments.id"), nullable=False),
        sa.Column("market_id", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.String(128), sa.ForeignKey("market_snapshots.id"), nullable=False),
        sa.Column("rules_capture_id", sa.Uuid(), sa.ForeignKey("market_rules_captures.id"), nullable=False),
        sa.Column("revision_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("baseline", JSON_VARIANT, nullable=False),
        sa.Column("activated_by", sa.String(160), nullable=False),
    )
    op.create_table(
        "revisit_captures", _id(), _created(),
        sa.Column("revision_id", sa.Uuid(), sa.ForeignKey("thesis_revisions.id"), nullable=False),
        sa.Column("market_id", sa.String(128), nullable=False),
        sa.Column("source_kind", sa.String(48), nullable=False),
        sa.Column("source_ref", sa.String(256), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_digest", sa.String(64), nullable=False),
        sa.Column("evidence", JSON_VARIANT, nullable=False),
        sa.UniqueConstraint("revision_id", "capture_digest", name="uq_revisit_capture_digest"),
    )
    op.create_table(
        "monitoring_observations", _id(), _created(),
        sa.Column("revision_id", sa.Uuid(), sa.ForeignKey("thesis_revisions.id"), nullable=False),
        sa.Column("capture_id", sa.Uuid(), sa.ForeignKey("revisit_captures.id"), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("comparison", JSON_VARIANT, nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.UniqueConstraint("revision_id", "capture_id", name="uq_revisit_observation_capture"),
    )
    op.create_table(
        "revisit_requests", _id(), _created(),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("revision_id", sa.Uuid(), sa.ForeignKey("thesis_revisions.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("capture_id", sa.Uuid(), sa.ForeignKey("revisit_captures.id"), nullable=False),
        sa.Column("observation_id", sa.Uuid(), sa.ForeignKey("monitoring_observations.id"), nullable=False),
        sa.UniqueConstraint("user_id", "revision_id", "idempotency_key", name="uq_revisit_request_key"),
    )
    op.create_table(
        "revisit_packets", _id(), _created(),
        sa.Column("revision_id", sa.Uuid(), sa.ForeignKey("thesis_revisions.id"), nullable=False),
        sa.Column("observation_id", sa.Uuid(), sa.ForeignKey("monitoring_observations.id"), nullable=False, unique=True),
        sa.Column("packet_digest", sa.String(64), nullable=False),
    )
    op.create_table(
        "human_revisit_decisions", _id(), _created(),
        sa.Column("packet_id", sa.Uuid(), sa.ForeignKey("revisit_packets.id"), nullable=False, unique=True),
        sa.Column("revision_id", sa.Uuid(), sa.ForeignKey("thesis_revisions.id"), nullable=False),
        sa.Column("observation_id", sa.Uuid(), sa.ForeignKey("monitoring_observations.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("actor_id", sa.String(160), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("decision_origin", sa.String(32), nullable=False),
        sa.Column("packet_digest", sa.String(64), nullable=False),
        sa.CheckConstraint("action IN ('dismiss', 'reaffirm')", name="ck_revisit_decision_action"),
    )


def downgrade() -> None:
    for name in (
        "human_revisit_decisions", "revisit_packets", "revisit_requests",
        "monitoring_observations", "revisit_captures", "thesis_revisions",
        "personal_theses",
    ):
        op.drop_table(name)
