"""Allow unmatched resolved alerts and enforce open-incident deduplication.

Revision ID: 0002_ingest_deduplication
Revises: 0001_initial_schema
"""
from alembic import op
from sqlalchemy import text

revision = "0002_ingest_deduplication"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("alert_occurrences", "incident_id", nullable=True)
    op.create_index(
        "uq_incidents_open_fingerprint",
        "incidents",
        ["fingerprint"],
        unique=True,
        postgresql_where=text("state <> 'RESOLVED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_incidents_open_fingerprint", table_name="incidents")
    op.execute("DELETE FROM alert_occurrences WHERE incident_id IS NULL")
    op.alter_column("alert_occurrences", "incident_id", nullable=False)
