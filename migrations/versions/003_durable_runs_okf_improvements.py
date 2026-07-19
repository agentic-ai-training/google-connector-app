"""Durable runs, operational knowledge, and governed improvements.

Revision ID: 003
Revises: 002
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


UPGRADE = r'''
CREATE TABLE agent_runs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    request TEXT NOT NULL,
    objective TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','awaiting_approval','running','completed','partial','failed','cancelled')),
    current_phase TEXT NOT NULL DEFAULT 'intake',
    current_step_id UUID,
    plan JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    incident_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    technical_completion NUMERIC(5,2) NOT NULL DEFAULT 0,
    functional_completion NUMERIC(5,2) NOT NULL DEFAULT 0,
    user_visible_completion NUMERIC(5,2) NOT NULL DEFAULT 0,
    side_effect_integrity NUMERIC(5,2) NOT NULL DEFAULT 100,
    risk_level TEXT NOT NULL DEFAULT 'low' CHECK (risk_level IN ('low','medium','high')),
    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
    approval_bypassed BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key TEXT NOT NULL,
    cancellation_source TEXT,
    error_category TEXT,
    error_message TEXT,
    langsmith_trace_id TEXT,
    models_used TEXT[] NOT NULL DEFAULT '{}',
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    prompt_version TEXT,
    okf_version TEXT,
    chunker_version TEXT,
    deployment_version TEXT,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    retention_until TIMESTAMPTZ NOT NULL DEFAULT now() + interval '90 days',
    deleted_at TIMESTAMPTZ,
    UNIQUE (user_id, idempotency_key)
);
CREATE INDEX agent_runs_session_idx ON agent_runs(user_id, session_id, queued_at DESC);
CREATE INDEX agent_runs_active_idx ON agent_runs(status, queued_at) WHERE status IN ('queued','running');
CREATE INDEX agent_runs_lease_idx ON agent_runs(lease_expires_at) WHERE status='running';

CREATE TABLE agent_run_steps (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_key TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    service TEXT,
    agent TEXT,
    tool_name TEXT,
    operation TEXT,
    dependencies TEXT[] NOT NULL DEFAULT '{}',
    read_only BOOLEAN NOT NULL DEFAULT TRUE,
    risk_level TEXT NOT NULL DEFAULT 'low' CHECK (risk_level IN ('low','medium','high')),
    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
    weight NUMERIC(8,4) NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','awaiting_approval','running','completed','failed','skipped','cancelled','compensated')),
    preconditions JSONB NOT NULL DEFAULT '[]'::jsonb,
    postconditions JSONB NOT NULL DEFAULT '[]'::jsonb,
    retry_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    input_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    duration_ms BIGINT,
    error_category TEXT,
    error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE(run_id, step_key),
    UNIQUE(run_id, sequence_no)
);
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_current_step_fk
  FOREIGN KEY (current_step_id) REFERENCES agent_run_steps(id) ON DELETE SET NULL;
CREATE INDEX agent_run_steps_status_idx ON agent_run_steps(run_id, status, sequence_no);

CREATE TABLE agent_run_events (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    phase TEXT,
    message TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX agent_run_events_replay_idx ON agent_run_events(run_id, id);
CREATE INDEX agent_run_events_user_idx ON agent_run_events(user_id, created_at DESC);

CREATE TABLE agent_artifacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
    user_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    external_id TEXT,
    url TEXT,
    name TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    sharing_state TEXT,
    cleanup_state TEXT NOT NULL DEFAULT 'retained',
    safe_to_delete BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at TIMESTAMPTZ,
    UNIQUE(run_id, artifact_type, external_id)
);
CREATE INDEX agent_artifacts_user_idx ON agent_artifacts(user_id, created_at DESC);

CREATE TABLE agent_model_calls (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
    component TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    queue_ms INTEGER,
    prompt_ms INTEGER,
    completion_ms INTEGER,
    fallback_from TEXT,
    error_category TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX agent_model_calls_run_idx ON agent_model_calls(run_id, created_at);

CREATE TABLE agent_tool_attempts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
    tool_name TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL,
    input_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    duration_ms INTEGER,
    error_category TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(run_id, step_id, attempt_no)
);

CREATE TABLE run_approvals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_id UUID REFERENCES agent_run_steps(id) ON DELETE CASCADE,
    requested_from TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    action_summary JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','expired')),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_by TEXT,
    decision_note TEXT,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX run_approval_pending_idx ON run_approvals(run_id, action_hash) WHERE status='pending';

CREATE TABLE rag_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    parent_id TEXT,
    chunk_index INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    acl JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(768),
    embedding_version TEXT,
    chunker_version TEXT NOT NULL,
    sync_version TEXT,
    source_modified_at TIMESTAMPTZ,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE(user_id, source_type, source_id, chunker_version, chunk_index)
);
CREATE INDEX rag_chunks_embedding_idx ON rag_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX rag_chunks_source_idx ON rag_chunks(user_id, source_type, source_id);

CREATE TABLE okf_documents (
    id TEXT PRIMARY KEY,
    visibility TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','private')),
    concept_type TEXT NOT NULL,
    title TEXT,
    description TEXT,
    resource TEXT,
    tags TEXT[] NOT NULL DEFAULT '{}',
    owner TEXT,
    version TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    trusted BOOLEAN NOT NULL DEFAULT FALSE,
    published_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX okf_documents_lookup_idx ON okf_documents(concept_type, trusted, updated_at DESC);
CREATE INDEX okf_documents_tags_idx ON okf_documents USING GIN(tags);

CREATE TABLE okf_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id TEXT NOT NULL REFERENCES okf_documents(id) ON DELETE CASCADE,
    heading TEXT,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding vector(768),
    chunker_version TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(document_id, chunker_version, chunk_index)
);
CREATE INDEX okf_chunks_embedding_idx ON okf_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE okf_links (
    source_id TEXT NOT NULL REFERENCES okf_documents(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES okf_documents(id) ON DELETE CASCADE,
    context TEXT,
    PRIMARY KEY(source_id, target_id)
);

CREATE TABLE improvement_proposals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_key TEXT NOT NULL UNIQUE,
    proposal_type TEXT NOT NULL,
    title TEXT NOT NULL,
    sanitized_summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'drafted',
    severity TEXT NOT NULL DEFAULT 'medium',
    risk_level TEXT NOT NULL DEFAULT 'medium',
    root_cause_confidence NUMERIC(5,2),
    affected_sessions INTEGER NOT NULL DEFAULT 0,
    exact_diff TEXT,
    expected_impact JSONB NOT NULL DEFAULT '{}'::jsonb,
    privacy_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    security_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    rollback_plan JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_version TEXT,
    candidate_version TEXT,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'analysis-system',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX improvement_review_idx ON improvement_proposals(status, severity, created_at DESC);

CREATE TABLE improvement_evidence (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_id UUID NOT NULL REFERENCES improvement_proposals(id) ON DELETE CASCADE,
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    evidence_type TEXT NOT NULL,
    sanitized_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE improvement_evaluations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_id UUID NOT NULL REFERENCES improvement_proposals(id) ON DELETE CASCADE,
    suite_version TEXT NOT NULL,
    control_metrics JSONB NOT NULL,
    candidate_metrics JSONB NOT NULL,
    regressions JSONB NOT NULL DEFAULT '[]'::jsonb,
    passed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE improvement_approvals (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_id UUID NOT NULL REFERENCES improvement_proposals(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('canary','promotion')),
    proposal_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved','rejected','changes_requested')),
    decided_by TEXT NOT NULL,
    decision_note TEXT,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE improvement_canaries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_id UUID NOT NULL REFERENCES improvement_proposals(id) ON DELETE CASCADE,
    cohort JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    control_version TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    rollback_reason TEXT
);

CREATE TABLE learning_trajectories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    consented BOOLEAN NOT NULL DEFAULT FALSE,
    sanitized BOOLEAN NOT NULL DEFAULT FALSE,
    state JSONB NOT NULL,
    decision JSONB NOT NULL,
    action JSONB NOT NULL,
    observation JSONB NOT NULL,
    reward JSONB NOT NULL,
    next_state JSONB NOT NULL,
    dataset_split TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE feature_flags (
    name TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by TEXT NOT NULL DEFAULT 'system',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO feature_flags(name, enabled) VALUES
 ('durable_runs', TRUE), ('legacy_chat', TRUE), ('okf', TRUE),
 ('governed_improvements', TRUE), ('new_rag', FALSE)
ON CONFLICT(name) DO NOTHING;

CREATE SCHEMA IF NOT EXISTS reporting;
CREATE OR REPLACE VIEW reporting.session_summary AS
SELECT r.id AS run_id, r.session_id, r.user_id, r.request, r.status,
       r.current_phase, r.technical_completion, r.functional_completion,
       r.user_visible_completion, r.side_effect_integrity, r.risk_level,
       r.queued_at, r.started_at, r.completed_at, r.heartbeat_at,
       r.error_category, r.incident_summary, r.models_used,
       r.input_tokens + r.output_tokens AS total_tokens,
       EXTRACT(EPOCH FROM (COALESCE(r.completed_at, now()) - COALESCE(r.started_at, r.queued_at))) * 1000 AS duration_ms,
       r.prompt_version, r.okf_version, r.chunker_version, r.deployment_version
FROM agent_runs r WHERE r.deleted_at IS NULL;

CREATE OR REPLACE VIEW reporting.step_timeline AS
SELECT s.run_id, s.id AS step_id, s.sequence_no, s.step_key, s.title, s.service,
       s.agent, s.tool_name, s.status, s.risk_level, s.requires_approval,
       s.duration_ms, s.error_category, s.error_message, s.started_at, s.completed_at
FROM agent_run_steps s;

CREATE OR REPLACE VIEW reporting.failure_summary_daily AS
SELECT date_trunc('day', queued_at) AS day, COALESCE(error_category, 'none') AS error_category,
       status, count(*) AS runs, avg(functional_completion) AS average_completion
FROM agent_runs GROUP BY 1,2,3;

CREATE OR REPLACE VIEW reporting.improvement_queue AS
SELECT proposal_key, proposal_type, title, status, severity, risk_level,
       affected_sessions, root_cause_confidence, source_version,
       candidate_version, created_at, expires_at
FROM improvement_proposals;
'''


DOWNGRADE = r'''
DROP SCHEMA IF EXISTS reporting CASCADE;
DROP TABLE IF EXISTS feature_flags;
DROP TABLE IF EXISTS learning_trajectories;
DROP TABLE IF EXISTS improvement_canaries;
DROP TABLE IF EXISTS improvement_approvals;
DROP TABLE IF EXISTS improvement_evaluations;
DROP TABLE IF EXISTS improvement_evidence;
DROP TABLE IF EXISTS improvement_proposals;
DROP TABLE IF EXISTS okf_links;
DROP TABLE IF EXISTS okf_chunks;
DROP TABLE IF EXISTS okf_documents;
DROP TABLE IF EXISTS rag_chunks;
DROP TABLE IF EXISTS run_approvals;
DROP TABLE IF EXISTS agent_tool_attempts;
DROP TABLE IF EXISTS agent_model_calls;
DROP TABLE IF EXISTS agent_artifacts;
DROP TABLE IF EXISTS agent_run_events;
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_current_step_fk;
DROP TABLE IF EXISTS agent_run_steps;
DROP TABLE IF EXISTS agent_runs;
'''


def upgrade():
    op.execute(UPGRADE)


def downgrade():
    op.execute(DOWNGRADE)
