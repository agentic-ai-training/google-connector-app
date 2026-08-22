"""Durable, version-pinned coding-agent runs and approval evidence.

Revision ID: 015
Revises: 014
"""

from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
CREATE TABLE coding_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    repository TEXT NOT NULL CHECK(repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
    base_ref TEXT NOT NULL DEFAULT 'main',
    base_commit TEXT,
    request_excerpt TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
    encrypted_request TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN (
      'queued','planning','awaiting_approval','approved','executing',
      'published','ci_running','completed','failed','blocked','cancelled','reconciling'
    )),
    current_phase TEXT NOT NULL DEFAULT 'intake',
    executor_version TEXT NOT NULL,
    planner_model TEXT NOT NULL,
    model_policy_version TEXT NOT NULL,
    tool_policy_version TEXT NOT NULL,
    okf_bundle_version TEXT,
    okf_document_ids TEXT[] NOT NULL DEFAULT '{}'::text[],
    okf_selection_reason TEXT,
    source_egress_consented_at TIMESTAMPTZ NOT NULL,
    plan_hash TEXT CHECK(plan_hash IS NULL OR plan_hash ~ '^[0-9a-f]{64}$'),
    encrypted_plan TEXT,
    encrypted_approval_manifest TEXT,
    approval_status TEXT NOT NULL DEFAULT 'not_requested'
      CHECK(approval_status IN ('not_requested','pending','approved','rejected','expired')),
    approval_action_hash TEXT,
    approval_requested_at TIMESTAMPTZ,
    approval_expires_at TIMESTAMPTZ,
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    publication_status TEXT NOT NULL DEFAULT 'not_started'
      CHECK(publication_status IN ('not_started','draft_pr','ci_running','ci_passed','ci_failed','cancelled')),
    branch_name TEXT,
    pull_request_number INTEGER,
    pull_request_url TEXT,
    candidate_commit TEXT,
    ci_check_url TEXT,
    input_tokens BIGINT NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
    output_tokens BIGINT NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 10),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    error_category TEXT,
    error_message TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    retention_until TIMESTAMPTZ NOT NULL DEFAULT now() + interval '90 days',
    deleted_at TIMESTAMPTZ,
    UNIQUE(user_id,idempotency_key)
);
CREATE INDEX coding_runs_claim_idx
  ON coding_runs(executor_version,status,available_at,lease_expires_at)
  WHERE status IN ('queued','approved','planning','executing');
CREATE INDEX coding_runs_tenant_idx
  ON coding_runs(user_id,created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX coding_runs_repository_idx
  ON coding_runs(repository,base_commit,created_at DESC);

CREATE TABLE coding_run_steps (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL REFERENCES coding_runs(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL CHECK(sequence_no >= 0),
    phase TEXT NOT NULL,
    tool_name TEXT,
    status TEXT NOT NULL CHECK(status IN (
      'pending','running','completed','failed','blocked','cancelled','superseded'
    )),
    request_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_hash TEXT,
    result_hash TEXT,
    duration_ms BIGINT CHECK(duration_ms IS NULL OR duration_ms >= 0),
    input_tokens BIGINT NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
    output_tokens BIGINT NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
    error_category TEXT,
    error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE(run_id,sequence_no)
);
CREATE INDEX coding_run_steps_timeline_idx ON coding_run_steps(run_id,sequence_no);

CREATE TABLE coding_run_events (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES coding_runs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    message TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX coding_run_events_replay_idx ON coding_run_events(run_id,id);
CREATE INDEX coding_run_events_tenant_idx ON coding_run_events(user_id,created_at DESC);

CREATE TABLE coding_artifacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL REFERENCES coding_runs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL CHECK(artifact_type IN (
      'plan','approval_manifest','diff','validation','checkpoint','branch','pull_request','ci_attestation'
    )),
    content_hash TEXT NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
    encrypted_payload TEXT,
    external_url TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    verification_status TEXT NOT NULL DEFAULT 'unverified'
      CHECK(verification_status IN ('unverified','verified','failed','superseded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at TIMESTAMPTZ,
    UNIQUE(run_id,artifact_type,content_hash)
);
CREATE INDEX coding_artifacts_tenant_idx ON coding_artifacts(user_id,created_at DESC);

CREATE TABLE coding_cache_entries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    cache_key TEXT NOT NULL CHECK(cache_key ~ '^[0-9a-f]{64}$'),
    producer_version TEXT NOT NULL,
    source_version TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
    encrypted_payload TEXT NOT NULL,
    policy_reason TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    invalidated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_count BIGINT NOT NULL DEFAULT 0 CHECK(access_count >= 0),
    UNIQUE(user_id,entity_type,scope_key,cache_key)
);
CREATE INDEX coding_cache_lookup_idx
  ON coding_cache_entries(user_id,entity_type,scope_key,cache_key,expires_at)
  WHERE invalidated_at IS NULL;
CREATE INDEX coding_cache_expiry_idx ON coding_cache_entries(expires_at);

ALTER TABLE candidate_builds
  ADD COLUMN coding_run_id UUID REFERENCES coding_runs(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX candidate_builds_coding_run_idx
  ON candidate_builds(coding_run_id) WHERE coding_run_id IS NOT NULL;

CREATE OR REPLACE VIEW reporting.coding_run_status AS
SELECT r.id,r.user_id,r.repository,r.base_ref,r.base_commit,r.request_excerpt,r.status,
       r.current_phase,r.executor_version,r.planner_model,r.model_policy_version,
       r.tool_policy_version,r.okf_bundle_version,r.okf_document_ids,
       r.okf_selection_reason,r.approval_status,r.publication_status,r.pull_request_url,
       r.candidate_commit,r.ci_check_url,
       r.input_tokens,r.output_tokens,r.attempt_count,r.error_category,r.error_message,
       r.created_at,r.updated_at,r.started_at,r.completed_at,
       count(DISTINCT s.id) AS step_count,count(DISTINCT a.id) AS artifact_count
FROM coding_runs r
LEFT JOIN coding_run_steps s ON s.run_id=r.id
LEFT JOIN coding_artifacts a ON a.run_id=r.id
WHERE r.deleted_at IS NULL
GROUP BY r.id;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') THEN
    GRANT SELECT ON reporting.coding_run_status TO dbeaver_analyst;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grafana_reader') THEN
    GRANT SELECT ON reporting.coding_run_status TO grafana_reader;
  END IF;
END $$;
''')


def downgrade():
    op.execute(r'''
DROP VIEW IF EXISTS reporting.coding_run_status;
ALTER TABLE candidate_builds DROP COLUMN IF EXISTS coding_run_id;
DROP TABLE IF EXISTS coding_artifacts;
DROP TABLE IF EXISTS coding_cache_entries;
DROP TABLE IF EXISTS coding_run_events;
DROP TABLE IF EXISTS coding_run_steps;
DROP TABLE IF EXISTS coding_runs;
''')
