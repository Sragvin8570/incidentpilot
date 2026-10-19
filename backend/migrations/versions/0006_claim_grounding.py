"""Persist per-claim target grounding details.

Revision ID: 0006_claim_grounding
Revises: 0005_decisions
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_claim_grounding"
down_revision = "0005_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "decisions",
        sa.Column(
            "claim_grounding",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
    )
    op.alter_column("decisions", "claim_grounding", server_default=None)


def downgrade() -> None:
    op.drop_column("decisions", "claim_grounding")
