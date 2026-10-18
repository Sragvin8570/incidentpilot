"""Persist validated RCA and embedded runbook sections.

Revision ID: 0004_rca_runbooks
Revises: 0003_evidence
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0004_rca_runbooks"
down_revision = "0003_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("incidents", sa.Column("rca_json", sa.JSON(), nullable=True))
    op.create_table(
        "runbook_chunks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("runbook_id", sa.String(length=128), nullable=False),
        sa.Column("action_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("heading", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runbook_chunks_runbook_id", "runbook_chunks", ["runbook_id"])
    op.create_index("ix_runbook_chunks_action_id", "runbook_chunks", ["action_id"])
    op.execute(
        "CREATE INDEX ix_runbook_chunks_embedding ON runbook_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_runbook_chunks_embedding")
    op.drop_index("ix_runbook_chunks_action_id", table_name="runbook_chunks")
    op.drop_index("ix_runbook_chunks_runbook_id", table_name="runbook_chunks")
    op.drop_table("runbook_chunks")
    op.drop_column("incidents", "rca_json")
