"""Add isolated anonymous demo sessions.

Revision ID: 4f7c2e9a6b13
Revises: 18e9c4a7d2b5
"""

from alembic import context, op
import sqlalchemy as sa

revision = "4f7c2e9a6b13"
down_revision = "18e9c4a7d2b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("fit_cards") as batch:
        batch.alter_column("semantic_fit_class", existing_type=sa.String(32),
                           nullable=True)
        batch.create_check_constraint(
            "ck_fit_card_demo_unassessed_only",
            "semantic_fit_class IS NOT NULL OR "
            "COALESCE(provenance ->> 'reconstructed', '') = "
            "'public-demo-hormuz-reconstruction-v1'",
        )
    with op.batch_alter_table("market_assessments") as batch:
        batch.drop_constraint("ck_market_assessment_pair_class", type_="check")
        batch.alter_column("pair_class", existing_type=sa.String(32), nullable=True)
        batch.create_check_constraint(
            "ck_market_assessment_pair_class",
            "(authority = 'demo_reconstruction_only' AND pair_class IS NULL) OR "
            "(authority <> 'demo_reconstruction_only' AND pair_class IS NOT NULL "
            "AND pair_class IN ('direct', 'indirect', 'weak_proxy', 'not_an_expression'))",
        )
    with op.batch_alter_table("market_display_sets") as batch:
        batch.drop_constraint("ck_market_display_count_within_assessed", type_="check")
        batch.create_check_constraint(
            "ck_market_display_count_within_assessed",
            "displayed_count <= assessed_count OR "
            "(display_policy_version = 'public-demo-hormuz-reconstruction-v1' "
            "AND assessed_count = 0 AND displayed_count = 1 AND target_count = 1 "
            "AND assessment_complete = FALSE AND system_pool_outcome = 'incomplete')",
        )
    op.create_table(
        "demo_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "cannot downgrade 4f7c2e9a6b13: offline downgrade SQL generation "
            "is unavailable because a live data preflight is required"
        )

    # SQLite batch DDL is non-transactional. Refuse before any mutation rather
    # than delete or fabricate successor-only semantic evidence.
    bind = op.get_bind()
    incompatible_tables = []
    predecessor_checks = (
        (
            "market_display_sets",
            "SELECT 1 FROM market_display_sets "
            "WHERE displayed_count > assessed_count LIMIT 1",
        ),
        (
            "market_assessments",
            "SELECT 1 FROM market_assessments "
            "WHERE pair_class IS NULL OR pair_class NOT IN "
            "('direct', 'indirect', 'weak_proxy', 'not_an_expression') LIMIT 1",
        ),
        (
            "fit_cards",
            "SELECT 1 FROM fit_cards WHERE semantic_fit_class IS NULL LIMIT 1",
        ),
    )
    for table_name, statement in predecessor_checks:
        if bind.execute(sa.text(statement)).first() is not None:
            incompatible_tables.append(table_name)
    if incompatible_tables:
        raise RuntimeError(
            "cannot downgrade 4f7c2e9a6b13: predecessor-incompatible "
            "anonymous demo rows exist in " + ", ".join(incompatible_tables)
        )

    with op.batch_alter_table("market_display_sets") as batch:
        batch.drop_constraint("ck_market_display_count_within_assessed", type_="check")
        batch.create_check_constraint(
            "ck_market_display_count_within_assessed",
            "displayed_count <= assessed_count",
        )
    with op.batch_alter_table("market_assessments") as batch:
        batch.drop_constraint("ck_market_assessment_pair_class", type_="check")
        batch.alter_column("pair_class", existing_type=sa.String(32), nullable=False)
        batch.create_check_constraint(
            "ck_market_assessment_pair_class",
            "pair_class IN ('direct', 'indirect', 'weak_proxy', 'not_an_expression')",
        )
    with op.batch_alter_table("fit_cards") as batch:
        batch.drop_constraint("ck_fit_card_demo_unassessed_only", type_="check")
        batch.alter_column("semantic_fit_class", existing_type=sa.String(32),
                           nullable=False)
    op.drop_table("demo_sessions")
