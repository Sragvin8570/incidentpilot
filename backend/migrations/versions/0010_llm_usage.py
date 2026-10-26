"""Record per-incident LLM token usage for evaluations.

Revision ID: 0010_llm_usage
Revises: 0009_verifications
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_llm_usage"
down_revision = "0009_verifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "prompt_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usage_incident_id", "llm_usage", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_incident_id", table_name="llm_usage")
    op.drop_table("llm_usage")
