"""Governance feedback, retrieval audit, and retention jobs.

Revision ID: 004
Revises: 003
"""

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
ALTER TABLE feedback ADD COLUMN run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL;
ALTER TABLE feedback ADD COLUMN step_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL;
ALTER TABLE feedback ADD COLUMN categories TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE feedback ADD COLUMN consented_for_learning BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE feedback ADD COLUMN sanitized BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE feedback ADD COLUMN expected_result TEXT;
CREATE INDEX feedback_run_idx ON feedback(user_id, run_id, created_at DESC);

CREATE TABLE embedding_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
      CHECK(status IN ('queued','running','completed','failed','dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE(user_id,source_type,source_id,content_hash)
);
CREATE INDEX embedding_jobs_claim_idx ON embedding_jobs(status,available_at)
  WHERE status IN ('queued','failed');

CREATE TABLE rag_retrieval_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    step_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
    user_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    reason TEXT,
    query_hash TEXT NOT NULL,
    returned_count INTEGER NOT NULL DEFAULT 0,
    used_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    source_types TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX rag_retrieval_user_idx ON rag_retrieval_events(user_id,created_at DESC);

CREATE TABLE okf_retrieval_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    step_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
    document_ids TEXT[] NOT NULL DEFAULT '{}',
    okf_versions TEXT[] NOT NULL DEFAULT '{}',
    query_hash TEXT NOT NULL,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE retention_audit (
    id BIGSERIAL PRIMARY KEY,
    policy_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    affected_rows INTEGER NOT NULL,
    action TEXT NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE deletion_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK(status IN ('pending','running','completed','failed')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    report JSONB NOT NULL DEFAULT '{}'::jsonb
);

INSERT INTO feature_flags(name,enabled,config) VALUES
 ('new_rag',TRUE,'{}'),
 ('shadow_planning',TRUE,'{}'),
 ('pilot_cohorts',FALSE,jsonb_build_object('stages',jsonb_build_array(10,30,50,90))),
 ('automatic_canary_rollback',TRUE,'{}'),
 ('live_rl',FALSE,jsonb_build_object('locked',TRUE))
ON CONFLICT(name) DO UPDATE SET enabled=EXCLUDED.enabled,config=EXCLUDED.config;

CREATE OR REPLACE VIEW reporting.model_token_usage AS
SELECT date_trunc('day',created_at) AS day, model, component, status,
       count(*) AS calls, sum(coalesce(input_tokens,0)) AS input_tokens,
       sum(coalesce(output_tokens,0)) AS output_tokens,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY completion_ms) AS p95_ms
FROM agent_model_calls GROUP BY 1,2,3,4;

CREATE OR REPLACE VIEW reporting.tool_reliability AS
SELECT date_trunc('day',created_at) AS day,tool_name,status,count(*) AS attempts,
       avg(duration_ms) AS avg_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
FROM agent_tool_attempts GROUP BY 1,2,3;

CREATE OR REPLACE VIEW reporting.retrieval_quality AS
SELECT date_trunc('day',created_at) AS day,mode,count(*) AS requests,
       avg(returned_count) AS avg_returned,avg(used_count) AS avg_used,
       avg(duration_ms) AS avg_ms
FROM rag_retrieval_events GROUP BY 1,2;
''')


def downgrade():
    op.execute(r'''
DROP VIEW IF EXISTS reporting.retrieval_quality;
DROP VIEW IF EXISTS reporting.tool_reliability;
DROP VIEW IF EXISTS reporting.model_token_usage;
DROP TABLE IF EXISTS deletion_requests;
DROP TABLE IF EXISTS retention_audit;
DROP TABLE IF EXISTS okf_retrieval_events;
DROP TABLE IF EXISTS rag_retrieval_events;
DROP TABLE IF EXISTS embedding_jobs;
DROP INDEX IF EXISTS feedback_run_idx;
ALTER TABLE feedback DROP COLUMN IF EXISTS expected_result;
ALTER TABLE feedback DROP COLUMN IF EXISTS sanitized;
ALTER TABLE feedback DROP COLUMN IF EXISTS consented_for_learning;
ALTER TABLE feedback DROP COLUMN IF EXISTS categories;
ALTER TABLE feedback DROP COLUMN IF EXISTS step_id;
ALTER TABLE feedback DROP COLUMN IF EXISTS run_id;
''')
