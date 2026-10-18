"""Create initial incident and audit tables.

Revision ID: 0001_initial_schema
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

incident_state = sa.Enum(
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
    create_type=False,
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    incident_state.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("alert_name", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=True),
        sa.Column("namespace", sa.String(length=253), nullable=True),
        sa.Column("environment", sa.String(length=32), nullable=True),
        sa.Column("priority", sa.String(length=8), nullable=True),
        sa.Column("target_kind", sa.String(length=64), nullable=True),
        sa.Column("target_name", sa.String(length=253), nullable=True),
        sa.Column("state", incident_state, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incidents_fingerprint", "incidents", ["fingerprint"])
    op.create_index("ix_incidents_state", "incidents", ["state"])
    op.create_table(
        "alert_occurrences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alert_occurrences_incident_id", "alert_occurrences", ["incident_id"]
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_incident_id", "audit_events", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_incident_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_alert_occurrences_incident_id", table_name="alert_occurrences")
    op.drop_table("alert_occurrences")
    op.drop_index("ix_incidents_state", table_name="incidents")
    op.drop_index("ix_incidents_fingerprint", table_name="incidents")
    op.drop_table("incidents")
    incident_state.drop(op.get_bind(), checkfirst=True)
