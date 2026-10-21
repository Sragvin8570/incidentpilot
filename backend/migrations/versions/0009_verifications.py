"""Persist closed-loop verification results.

Revision ID: 0009_verifications
Revises: 0008_users
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_verifications"
down_revision = "0008_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("applied_spec_hash", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "verifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="RUNNING",
            nullable=False,
        ),
        sa.Column(
            "checks",
            postgresql.JSON(astext_type=sa.Text()),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column(
            "effectiveness_score",
            sa.Float(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "resolution_summary",
            sa.Text(),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["incidents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", name="uq_verifications_incident_id"),
    )
    op.create_index(
        "ix_verifications_incident_id", "verifications", ["incident_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_verifications_incident_id", table_name="verifications")
    op.drop_table("verifications")
    op.drop_column("executions", "applied_spec_hash")
