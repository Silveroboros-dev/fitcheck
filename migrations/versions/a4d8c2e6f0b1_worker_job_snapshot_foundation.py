"""worker job and immutable universe snapshot foundation

Revision ID: a4d8c2e6f0b1
Revises: f2a4c6e8b1d3
Create Date: 2026-08-06

Expand-only for existing product artifacts: historical candidate sets, fit
cards, and recommendations keep NULL job bindings. New worker writes can use
the exact job -> candidate set -> card -> recommendation chain without
fabricating provenance for old rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4d8c2e6f0b1"
down_revision: Union[str, Sequence[str], None] = "f2a4c6e8b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("owner_client_type", sa.String(length=32), nullable=False),
        sa.Column("owner_actor_id", sa.String(length=160), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("submitted_by_api_client_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_hash_version", sa.Integer(), nullable=False),
        sa.Column("payload", JSON_VARIANT, nullable=False),
        sa.Column("pinned_manifest", JSON_VARIANT, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", JSON_VARIANT, nullable=True),
        sa.Column("error_kind", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_error_message", sa.String(length=256), nullable=True),
        sa.Column("error_details", JSON_VARIANT, nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_jobs_attempt_nonnegative"
        ),
        sa.CheckConstraint(
            "max_attempts > 0", name="ck_jobs_max_attempts_positive"
        ),
        sa.CheckConstraint(
            "payload_hash_version > 0",
            name="ck_jobs_payload_hash_version_positive",
        ),
        sa.CheckConstraint(
            "attempt_count <= max_attempts", name="ck_jobs_attempt_within_budget"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', "
            "'failed', 'cancelled', 'needs_operator')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "((status = 'running' AND active_attempt_id IS NOT NULL "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'running' AND active_attempt_id IS NULL "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL))",
            name="ck_jobs_active_lease_shape",
        ),
        sa.CheckConstraint(
            "((status IN ('succeeded', 'failed', 'cancelled', "
            "'needs_operator') AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('succeeded', 'failed', 'cancelled', "
            "'needs_operator') AND completed_at IS NULL))",
            name="ck_jobs_completion_shape",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["submitted_by_api_client_id"], ["api_clients.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_client_type",
            "owner_actor_id",
            "job_type",
            "idempotency_key",
            name="uq_job_idempotency",
        ),
    )
    op.create_index(
        "ix_jobs_claimable",
        "jobs",
        ["job_type", "status", "priority", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_jobs_lease_expiry",
        "jobs",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_jobs_deadline",
        "jobs",
        ["status", "deadline_at"],
        unique=False,
    )

    op.create_table(
        "job_attempts",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_trace_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_kind", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_error_message", sa.String(length=256), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "attempt_number > 0", name="ck_job_attempt_number_positive"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'retry_wait', 'failed', "
            "'abandoned', 'cancelled')",
            name="ck_job_attempts_status",
        ),
        sa.CheckConstraint(
            "((status = 'running' AND finished_at IS NULL) OR "
            "(status <> 'running' AND finished_at IS NOT NULL))",
            name="ck_job_attempts_completion_shape",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "attempt_number", name="uq_job_attempt_number"
        ),
    )

    op.create_table(
        "market_universe_snapshots",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("cutoff_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("membership_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_uri", sa.String(length=2048), nullable=False),
        sa.Column("artifact_format", sa.String(length=32), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("unique_market_count", sa.Integer(), nullable=False),
        sa.Column("open_market_count", sa.Integer(), nullable=False),
        sa.Column(
            "normalization_policy_version", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "validation_policy_version", sa.String(length=64), nullable=False
        ),
        sa.Column("source_versions", JSON_VARIANT, nullable=False),
        sa.Column("manifest", JSON_VARIANT, nullable=False),
        sa.Column("created_by_job_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "artifact_bytes >= 0", name="ck_universe_artifact_bytes"
        ),
        sa.CheckConstraint("row_count > 0", name="ck_universe_row_count"),
        sa.CheckConstraint(
            "unique_market_count = row_count", name="ck_universe_unique_rows"
        ),
        sa.CheckConstraint(
            "open_market_count >= 0 AND open_market_count <= row_count",
            name="ck_universe_open_count",
        ),
        sa.ForeignKeyConstraint(["created_by_job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "venue",
            "cutoff_utc",
            "content_sha256",
            "normalization_policy_version",
            name="uq_market_universe_content",
        ),
        sa.UniqueConstraint(
            "provider",
            "venue",
            "id",
            name="uq_market_universe_key_id",
        ),
    )

    op.create_table(
        "active_market_universes",
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("promoted_by_job_id", sa.Uuid(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "generation > 0", name="ck_active_universe_generation"
        ),
        sa.ForeignKeyConstraint(
            ["provider", "venue", "snapshot_id"],
            [
                "market_universe_snapshots.provider",
                "market_universe_snapshots.venue",
                "market_universe_snapshots.id",
            ],
            name="fk_active_universe_snapshot_identity",
        ),
        sa.ForeignKeyConstraint(["promoted_by_job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("provider", "venue"),
    )

    with op.batch_alter_table("candidate_sets") as batch:
        batch.add_column(sa.Column("retrieval_id", sa.String(length=128)))
        batch.add_column(sa.Column("job_id", sa.Uuid()))
        batch.create_foreign_key("fk_candidate_sets_job", "jobs", ["job_id"], ["id"])
        batch.create_unique_constraint("uq_candidate_set_job", ["job_id"])

    with op.batch_alter_table("fit_cards") as batch:
        batch.add_column(sa.Column("job_id", sa.Uuid()))
        batch.create_foreign_key("fk_fit_cards_job", "jobs", ["job_id"], ["id"])
        batch.create_unique_constraint("uq_fit_card_job", ["job_id"])

    with op.batch_alter_table("market_recommendations") as batch:
        batch.add_column(sa.Column("fit_card_id", sa.Uuid()))
        batch.add_column(sa.Column("job_id", sa.Uuid()))
        batch.create_foreign_key(
            "fk_market_recommendations_fit_card",
            "fit_cards",
            ["fit_card_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "fk_market_recommendations_job", "jobs", ["job_id"], ["id"]
        )
        batch.create_unique_constraint(
            "uq_recommendation_fit_card", ["fit_card_id"]
        )
        batch.create_unique_constraint("uq_recommendation_job", ["job_id"])


def downgrade() -> None:
    with op.batch_alter_table("market_recommendations") as batch:
        batch.drop_constraint("uq_recommendation_job", type_="unique")
        batch.drop_constraint("uq_recommendation_fit_card", type_="unique")
        batch.drop_constraint("fk_market_recommendations_job", type_="foreignkey")
        batch.drop_constraint(
            "fk_market_recommendations_fit_card", type_="foreignkey"
        )
        batch.drop_column("job_id")
        batch.drop_column("fit_card_id")

    with op.batch_alter_table("fit_cards") as batch:
        batch.drop_constraint("uq_fit_card_job", type_="unique")
        batch.drop_constraint("fk_fit_cards_job", type_="foreignkey")
        batch.drop_column("job_id")

    with op.batch_alter_table("candidate_sets") as batch:
        batch.drop_constraint("uq_candidate_set_job", type_="unique")
        batch.drop_constraint("fk_candidate_sets_job", type_="foreignkey")
        batch.drop_column("job_id")
        batch.drop_column("retrieval_id")

    op.drop_table("active_market_universes")
    op.drop_table("market_universe_snapshots")
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_deadline", table_name="jobs")
    op.drop_index("ix_jobs_lease_expiry", table_name="jobs")
    op.drop_index("ix_jobs_claimable", table_name="jobs")
    op.drop_table("jobs")
