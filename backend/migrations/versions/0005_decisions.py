"""Persist policy decisions and add the ready-to-execute state.

Revision ID: 0005_decisions
Revises: 0004_rca_runbooks
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_decisions"
down_revision = "0004_rca_runbooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE incident_state ADD VALUE IF NOT EXISTS 'READY'")
    op.create_table(
        "decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recommended_action", sa.String(length=128), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("llm_confidence", sa.Float(), nullable=False),
        sa.Column("grounding_score", sa.Float(), nullable=False),
        sa.Column("runbook_score", sa.Float(), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=False),
        sa.Column("risk_reason", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", name="uq_decisions_incident_id"),
    )
    op.create_index("ix_decisions_incident_id", "decisions", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_decisions_incident_id", table_name="decisions")
    op.drop_table("decisions")
    op.execute("ALTER TYPE incident_state RENAME TO incident_state_old")
    old_state = postgresql.ENUM(
        "RECEIVED",
        "UNSCOPED",
        "ENRICHED",
        "INVESTIGATING",
        "ANALYZED",
        "AWAITING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "EXPIRED",
        "EXECUTING",
        "VERIFYING",
        "RESOLVED",
        "ESCALATED",
        "FAILED",
        name="incident_state",
    )
    old_state.create(op.get_bind())
    op.execute(
        "ALTER TABLE incidents ALTER COLUMN state TYPE incident_state USING state::text::incident_state"
    )
    op.execute("DROP TYPE incident_state_old")
