"""Evaluation, compensation, and notification governance.

Revision ID: 007
Revises: 006
"""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
ALTER TABLE prompt_experiments ADD COLUMN selection_policy TEXT NOT NULL DEFAULT 'ab'
  CHECK(selection_policy IN ('ab','thompson'));
ALTER TABLE prompt_experiments ADD COLUMN validated BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prompt_experiments ADD COLUMN validated_by TEXT;
ALTER TABLE prompt_experiments ADD COLUMN validated_at TIMESTAMPTZ;
ALTER TABLE prompt_experiments ADD COLUMN validation_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE workflow_evaluations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    suite_version TEXT NOT NULL,
    policy_name TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    candidate BOOLEAN NOT NULL DEFAULT FALSE,
    task_success NUMERIC(6,4),
    plan_correctness NUMERIC(6,4),
    tool_correctness NUMERIC(6,4),
    artifact_correctness NUMERIC(6,4),
    recovery_success NUMERIC(6,4),
    side_effect_integrity NUMERIC(6,4),
    user_satisfaction NUMERIC(6,4),
    retrieval_quality NUMERIC(6,4),
    latency_ms BIGINT,
    tokens BIGINT,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(run_id,policy_name,policy_version)
);
CREATE INDEX workflow_evaluations_policy_idx
  ON workflow_evaluations(policy_name,policy_version,created_at DESC);

CREATE TABLE policy_evaluation_reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    suite_version TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    method TEXT NOT NULL,
    eligible BOOLEAN NOT NULL DEFAULT FALSE,
    sample_size INTEGER NOT NULL DEFAULT 0,
    baseline_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    deltas JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    blocked_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE artifact_cleanup_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    artifact_id UUID NOT NULL REFERENCES agent_artifacts(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN
      ('preserve','delete','cancel_event','retry_population','rollback_sharing')),
    status TEXT NOT NULL DEFAULT 'awaiting_confirmation' CHECK(status IN
      ('awaiting_confirmation','approved','rejected','executing','completed','failed','manual_required')),
    action_hash TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now()+interval '30 minutes',
    decided_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    result JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX artifact_cleanup_pending_idx
  ON artifact_cleanup_requests(artifact_id,action)
  WHERE status IN ('awaiting_confirmation','approved','executing');

CREATE TABLE improvement_notifications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_id UUID NOT NULL REFERENCES improvement_proposals(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('admin','grafana','email','github')),
    event_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','sent','skipped','failed')),
    sanitized_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    external_reference TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    UNIQUE(proposal_id,channel,event_type)
);

CREATE OR REPLACE VIEW reporting.workflow_evaluation AS
SELECT date_trunc('day',created_at) AS day,policy_name,policy_version,candidate,
       count(*) AS samples,avg(task_success) AS task_success,
       avg(plan_correctness) AS plan_correctness,
       avg(tool_correctness) AS tool_correctness,
       avg(artifact_correctness) AS artifact_correctness,
       avg(recovery_success) AS recovery_success,
       avg(side_effect_integrity) AS side_effect_integrity,
       avg(user_satisfaction) AS user_satisfaction,
       avg(retrieval_quality) AS retrieval_quality,
       avg(latency_ms) AS avg_latency_ms,avg(tokens) AS avg_tokens
FROM workflow_evaluations GROUP BY 1,2,3,4;

CREATE OR REPLACE VIEW reporting.improvement_notifications AS
SELECT p.proposal_key,p.status AS proposal_status,n.channel,n.event_type,
       n.status,n.external_reference,n.error_message,n.created_at,n.sent_at
FROM improvement_notifications n
JOIN improvement_proposals p ON p.id=n.proposal_id;

CREATE OR REPLACE VIEW reporting.artifact_compensation AS
SELECT c.id,c.run_id,c.user_id,c.action,c.status,c.requested_at,c.decided_at,
       c.completed_at,c.error_message,a.artifact_type,a.external_id,a.cleanup_state
FROM artifact_cleanup_requests c JOIN agent_artifacts a ON a.id=c.artifact_id;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') THEN
    GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO dbeaver_analyst;
  END IF;
END $$;
''')


def downgrade():
    op.execute(r'''
DROP VIEW IF EXISTS reporting.artifact_compensation;
DROP VIEW IF EXISTS reporting.improvement_notifications;
DROP VIEW IF EXISTS reporting.workflow_evaluation;
DROP TABLE IF EXISTS improvement_notifications;
DROP TABLE IF EXISTS artifact_cleanup_requests;
DROP TABLE IF EXISTS policy_evaluation_reports;
DROP TABLE IF EXISTS workflow_evaluations;
ALTER TABLE prompt_experiments DROP COLUMN IF EXISTS validation_evidence;
ALTER TABLE prompt_experiments DROP COLUMN IF EXISTS validated_at;
ALTER TABLE prompt_experiments DROP COLUMN IF EXISTS validated_by;
ALTER TABLE prompt_experiments DROP COLUMN IF EXISTS validated;
ALTER TABLE prompt_experiments DROP COLUMN IF EXISTS selection_policy;
''')
