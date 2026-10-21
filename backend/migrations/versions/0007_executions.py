"""Persist phase 6 execution reservations and job results.

Revision ID: 0007_executions
Revises: 0006_claim_grounding
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_executions"
down_revision = "0006_claim_grounding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("job_name", sa.String(length=253), nullable=True),
        sa.Column("target_snapshot", sa.JSON(), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("patch_json", sa.JSON(), nullable=True),
        sa.Column("logs", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", name="uq_executions_incident_id"),
    )
    op.create_index("ix_executions_incident_id", "executions", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_executions_incident_id", table_name="executions")
    op.drop_table("executions")
