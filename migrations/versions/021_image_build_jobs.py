"""Durable image packaging ownership and shared resource obligations.

Revision ID: 021
Revises: 020
"""
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
CREATE TABLE coding_image_build_jobs (
    id UUID PRIMARY KEY,
    run_id UUID,
    user_id TEXT NOT NULL,
    validation_claim_id UUID NOT NULL REFERENCES coding_validation_claims(id),
    target_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    request JSONB NOT NULL,
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
      CHECK(status IN ('reserved','dispatched','uncertain','finished','retired')),
    cpu_millis INTEGER NOT NULL CHECK(cpu_millis=1000),
    memory_mib INTEGER NOT NULL CHECK(memory_mib=3072),
    storage_mib INTEGER NOT NULL CHECK(storage_mib=2048),
    planned_end_at TIMESTAMPTZ NOT NULL,
    output_expires_at TIMESTAMPTZ NOT NULL,
    dispatch_token UUID UNIQUE,
    worker_owner TEXT,
    dispatched_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    result_hash TEXT CHECK(result_hash ~ '^[0-9a-f]{64}$'),
    encrypted_result TEXT,
    result_recorded_at TIMESTAMPTZ,
    cleanup_evidence_hash TEXT CHECK(cleanup_evidence_hash ~ '^[0-9a-f]{64}$'),
    execution_released_at TIMESTAMPTZ,
    storage_released_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id,idempotency_key),
    UNIQUE(validation_claim_id,request_hash),
    FOREIGN KEY(run_id,user_id) REFERENCES coding_runs(id,user_id) ON DELETE SET NULL (run_id),
    FOREIGN KEY(target_id,user_id,project_id)
      REFERENCES coding_deployment_targets(target_id,user_id,project_id),
    FOREIGN KEY(pool_id,project_id) REFERENCES coding_host_capacity(pool_id,project_id),
    CHECK(output_expires_at>=planned_end_at),
    CHECK((dispatch_token IS NULL)=(worker_owner IS NULL)),
    CHECK((dispatch_token IS NULL)=(dispatched_at IS NULL)),
    CHECK((dispatch_token IS NULL)=(heartbeat_at IS NULL)),
    CHECK((result_hash IS NULL)=(result_recorded_at IS NULL)),
    CHECK(encrypted_result IS NULL OR result_hash IS NOT NULL),
    CHECK(status<>'reserved' OR dispatch_token IS NULL),
    CHECK(status<>'finished' OR (result_hash IS NOT NULL AND execution_released_at IS NOT NULL)),
    CHECK((status='retired')=(storage_released_at IS NOT NULL)),
    CHECK(storage_released_at IS NULL OR execution_released_at IS NOT NULL)
);
CREATE INDEX coding_image_build_capacity ON coding_image_build_jobs(pool_id)
  WHERE storage_released_at IS NULL;
CREATE TABLE coding_image_build_events (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES coding_image_build_jobs(id),
    event_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE FUNCTION scrub_orphan_image_build_result() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.run_id IS NULL THEN NEW.encrypted_result := NULL; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER image_build_run_deleted BEFORE INSERT OR UPDATE
  ON coding_image_build_jobs FOR EACH ROW EXECUTE FUNCTION scrub_orphan_image_build_result();
"""


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM coding_image_build_jobs WHERE storage_released_at IS NULL
        OR encrypted_result IS NOT NULL) THEN
        RAISE EXCEPTION 'reconcile image resources and expire private results before downgrade';
      END IF;
    END $$;
    DROP TABLE coding_image_build_events;
    DROP TABLE coding_image_build_jobs;
    DROP FUNCTION scrub_orphan_image_build_result();
    """)
