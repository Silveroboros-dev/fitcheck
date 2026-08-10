"""market_structures cached by rules content (ratification item 8)

Identity moves from (market_id, snapshot_id, schema_version) to
(market_id, contract_terms_hash, resolution_rules_hash, schema_version,
extraction_policy_version); snapshot_id becomes first-capture provenance.

Revision ID: 7c2e91ab4d10
Revises: 024a5fed003d
Create Date: 2026-06-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c2e91ab4d10"
down_revision: Union[str, Sequence[str], None] = "024a5fed003d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("market_structures") as batch:
        batch.add_column(
            sa.Column(
                "contract_terms_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )
        batch.add_column(
            sa.Column(
                "resolution_rules_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )
        batch.drop_constraint("uq_market_structure", type_="unique")
        batch.create_unique_constraint(
            "uq_market_structure",
            [
                "market_id",
                "contract_terms_hash",
                "resolution_rules_hash",
                "schema_version",
                "extraction_policy_version",
            ],
        )


def downgrade() -> None:
    with op.batch_alter_table("market_structures") as batch:
        batch.drop_constraint("uq_market_structure", type_="unique")
        batch.drop_column("resolution_rules_hash")
        batch.drop_column("contract_terms_hash")
        batch.create_unique_constraint(
            "uq_market_structure",
            ["market_id", "snapshot_id", "schema_version"],
        )
