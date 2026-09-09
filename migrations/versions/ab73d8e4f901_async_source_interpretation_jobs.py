"""async source interpretation request and result binding

Revision ID: ab73d8e4f901
Revises: f9a5b7c3d2e1
Create Date: 2026-09-05

Additive only. Existing synchronous source interpretations remain valid with
null request/job bindings. No review or golden data is touched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ab73d8e4f901"
down_revision: Union[str, Sequence[str], None] = "f9a5b7c3d2e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VARIANT = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "external_effect_started_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("external_effect_attempt_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_jobs_external_effect_binding",
            "((external_effect_started_at IS NULL AND "
            "external_effect_attempt_id IS NULL) OR "
            "(external_effect_started_at IS NOT NULL AND "
            "external_effect_attempt_id IS NOT NULL))",
        )
    op.create_table(
        "source_interpretation_requests",
        sa.Column("owner_client_type", sa.String(length=32), nullable=False),
        sa.Column("owner_actor_id", sa.String(length=160), nullable=False),
        sa.Column("agent_client_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("privacy_refusal_code", sa.String(length=64), nullable=True),
        sa.Column("pinned_manifest", JSON_VARIANT, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "((privacy_refusal_code IS NULL AND input_text IS NOT NULL "
            "AND input_digest IS NOT NULL) OR "
            "(privacy_refusal_code IS NOT NULL AND input_text IS NULL "
            "AND input_digest IS NULL AND source_url IS NULL))",
            name="ck_source_interpretation_request_privacy_shape",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_client_type",
            "owner_actor_id",
            "idempotency_key",
            name="uq_source_interpretation_request_idempotency",
        ),
    )
    with op.batch_alter_table("source_interpretations") as batch_op:
        batch_op.add_column(
            sa.Column("source_interpretation_request_id", sa.Uuid(), nullable=True)
        )
        batch_op.add_column(sa.Column("job_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_source_interpretation_request",
            "source_interpretation_requests",
            ["source_interpretation_request_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_source_interpretation_job", "jobs", ["job_id"], ["id"]
        )
        batch_op.create_unique_constraint(
            "uq_source_interpretation_request", ["source_interpretation_request_id"]
        )
        batch_op.create_unique_constraint(
            "uq_source_interpretation_job", ["job_id"]
        )
        batch_op.create_check_constraint(
            "ck_source_interpretation_async_binding",
            "((source_interpretation_request_id IS NULL AND job_id IS NULL) "
            "OR (source_interpretation_request_id IS NOT NULL "
            "AND job_id IS NOT NULL))",
        )


def downgrade() -> None:
    with op.batch_alter_table("source_interpretations") as batch_op:
        batch_op.drop_constraint(
            "ck_source_interpretation_async_binding", type_="check"
        )
        batch_op.drop_constraint("uq_source_interpretation_job", type_="unique")
        batch_op.drop_constraint("uq_source_interpretation_request", type_="unique")
        batch_op.drop_constraint("fk_source_interpretation_job", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_source_interpretation_request", type_="foreignkey"
        )
        batch_op.drop_column("job_id")
        batch_op.drop_column("source_interpretation_request_id")
    op.drop_table("source_interpretation_requests")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_constraint(
            "ck_jobs_external_effect_binding", type_="check"
        )
        batch_op.drop_column("external_effect_attempt_id")
        batch_op.drop_column("external_effect_started_at")
