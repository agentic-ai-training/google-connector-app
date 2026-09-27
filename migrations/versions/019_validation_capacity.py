"""Central validation claims share funded host capacity with deployments.

Revision ID: 019
Revises: 018
"""
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
ALTER TABLE coding_runs ADD CONSTRAINT coding_run_owner_key UNIQUE(id,user_id);
CREATE TABLE coding_validation_claims (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    user_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK(sequence_no>=0),
    target_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    request JSONB NOT NULL,
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
      CHECK(status IN ('reserved','dispatched','uncertain','released')),
    cpu_millis INTEGER NOT NULL CHECK(cpu_millis=1000),
    memory_mib INTEGER NOT NULL CHECK(memory_mib=1024),
    storage_mib INTEGER NOT NULL CHECK(storage_mib=64),
    planned_end_at TIMESTAMPTZ NOT NULL,
    host_job_id TEXT CHECK(host_job_id ~ '^[0-9a-f]{32}$'),
    evidence_hash TEXT CHECK(evidence_hash ~ '^[0-9a-f]{64}$'),
    released_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id,idempotency_key),
    UNIQUE(run_id,sequence_no,request_hash),
    UNIQUE(pool_id,host_job_id),
    FOREIGN KEY(run_id,user_id) REFERENCES coding_runs(id,user_id),
    FOREIGN KEY(target_id,user_id,project_id)
      REFERENCES coding_deployment_targets(target_id,user_id,project_id),
    FOREIGN KEY(pool_id,project_id) REFERENCES coding_host_capacity(pool_id,project_id),
    CHECK((status='released')=(released_at IS NOT NULL))
);
CREATE INDEX coding_validation_unreleased ON coding_validation_claims(pool_id)
    WHERE released_at IS NULL;
CREATE TABLE coding_validation_events (
    id BIGSERIAL PRIMARY KEY,
    claim_id UUID NOT NULL REFERENCES coding_validation_claims(id),
    event_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    # Do not discard the only ownership/cleanup evidence for dispatched resources.
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM coding_validation_claims WHERE released_at IS NULL) THEN
        RAISE EXCEPTION 'reconcile validation claims before downgrade';
      END IF;
    END $$;
    DROP TABLE coding_validation_events;
    DROP TABLE coding_validation_claims;
    ALTER TABLE coding_runs DROP CONSTRAINT coding_run_owner_key;
    """)
