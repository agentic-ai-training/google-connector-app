"""Durable, tenant-scoped source-aware RAG synchronization jobs.

Revision ID: 014
Revises: 013
"""

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
CREATE TABLE rag_source_sync_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    sources TEXT[] NOT NULL,
    max_items_per_source INTEGER NOT NULL
      CHECK(max_items_per_source BETWEEN 1 AND 100),
    requeue_known_failures BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'queued'
      CHECK(status IN ('queued','running','completed','failed','dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    result JSONB,
    error_message TEXT,
    consented_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
CREATE INDEX rag_source_sync_jobs_claim_idx
  ON rag_source_sync_jobs(status,available_at,lease_expires_at);
CREATE INDEX rag_source_sync_jobs_tenant_idx
  ON rag_source_sync_jobs(user_id,created_at DESC);
CREATE UNIQUE INDEX rag_source_sync_jobs_one_active_per_user
  ON rag_source_sync_jobs(user_id)
  WHERE status IN ('queued','running','failed');
''')


def downgrade():
    op.execute("DROP TABLE IF EXISTS rag_source_sync_jobs")
