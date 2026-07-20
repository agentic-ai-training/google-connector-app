"""Guarded intent routing and complete failure intelligence.

Revision ID: 010
Revises: 009
"""

from alembic import op


revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
ALTER TABLE agent_runs
  ADD COLUMN intent_kind TEXT NOT NULL DEFAULT 'workspace_action'
    CHECK(intent_kind IN ('workspace_action','workspace_guidance',
      'product_information','scope_chat','ambiguous','out_of_scope')),
  ADD COLUMN intent_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN failure_fingerprint TEXT,
  ADD COLUMN planning_diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE failure_incidents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    occurrence_key TEXT NOT NULL UNIQUE,
    run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    session_id TEXT,
    user_id TEXT NOT NULL,
    request_excerpt TEXT NOT NULL,
    request_shape JSONB NOT NULL DEFAULT '{}'::jsonb,
    intent_kind TEXT NOT NULL DEFAULT 'ambiguous',
    stage TEXT NOT NULL CHECK(stage IN
      ('intake','classification','planning','validation','admission','approval',
       'execution','verification','recovery','persistence','api')),
    category TEXT NOT NULL,
    component TEXT NOT NULL,
    service TEXT,
    operation TEXT,
    failure_fingerprint TEXT NOT NULL,
    cluster_key TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    contributing_factors JSONB NOT NULL DEFAULT '[]'::jsonb,
    breaking_point TEXT,
    completion JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    improvement_options JSONB NOT NULL,
    recommended_option TEXT NOT NULL CHECK(recommended_option IN ('A','B')),
    recommendation_reason TEXT NOT NULL,
    risk_level TEXT NOT NULL DEFAULT 'medium'
      CHECK(risk_level IN ('low','medium','high')),
    automation_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    analysis_status TEXT NOT NULL DEFAULT 'awaiting_review'
      CHECK(analysis_status IN
        ('awaiting_review','proposal_created','acknowledged','ignored')),
    proposal_id UUID REFERENCES improvement_proposals(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK(jsonb_typeof(improvement_options)='array'
          AND jsonb_array_length(improvement_options)=2)
);
CREATE INDEX failure_incidents_inbox_idx
  ON failure_incidents(analysis_status,risk_level,created_at DESC);
CREATE INDEX failure_incidents_cluster_idx
  ON failure_incidents(cluster_key,created_at DESC);
CREATE INDEX failure_incidents_run_idx ON failure_incidents(run_id);

CREATE TABLE failure_incident_reviews (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    incident_id UUID NOT NULL REFERENCES failure_incidents(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK(decision IN
      ('choose_A','choose_B','acknowledged','ignored')),
    selected_option TEXT CHECK(selected_option IN ('A','B')),
    decided_by TEXT NOT NULL,
    decision_note TEXT,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX failure_incident_reviews_incident_idx
  ON failure_incident_reviews(incident_id,decided_at DESC);

CREATE TABLE failure_incident_notifications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    incident_id UUID NOT NULL REFERENCES failure_incidents(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK(channel IN ('admin','grafana','email','github')),
    event_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','sent','skipped','failed')),
    sanitized_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    external_reference TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    UNIQUE(incident_id,channel,event_type)
);

ALTER TABLE improvement_proposals
  ADD COLUMN failure_cluster_key TEXT,
  ADD COLUMN selected_option TEXT CHECK(selected_option IN ('A','B'));
CREATE INDEX improvement_proposals_failure_cluster_idx
  ON improvement_proposals(failure_cluster_key,created_at DESC);

INSERT INTO feature_flags(name,enabled,config,updated_by)
VALUES('failure_improvement_automation',FALSE,
       jsonb_build_object('mode','manual','human_approval_required',TRUE),'system')
ON CONFLICT(name) DO NOTHING;

CREATE OR REPLACE VIEW reporting.failure_intelligence AS
SELECT i.id AS incident_id,i.run_id,i.session_id,i.user_id,i.intent_kind,i.stage,
       i.category,i.component,i.service,i.operation,i.failure_fingerprint,
       i.cluster_key,i.title,i.summary,i.root_cause,i.breaking_point,
       i.completion,i.recommended_option,i.recommendation_reason,i.risk_level,
       i.automation_eligible,i.analysis_status,i.proposal_id,i.created_at,i.updated_at
FROM failure_incidents i;

CREATE OR REPLACE VIEW reporting.failure_cluster_summary AS
SELECT cluster_key,stage,category,component,service,operation,
       count(*) AS occurrences,
       count(*) FILTER(WHERE analysis_status='awaiting_review') AS awaiting_review,
       min(created_at) AS first_seen,max(created_at) AS last_seen,
       max(risk_level) AS highest_risk
FROM failure_incidents
GROUP BY cluster_key,stage,category,component,service,operation;

CREATE OR REPLACE VIEW reporting.failure_notifications AS
SELECT i.id AS incident_id,i.cluster_key,i.title,i.analysis_status,
       n.channel,n.event_type,n.status,n.external_reference,n.error_message,
       n.created_at,n.sent_at
FROM failure_incident_notifications n
JOIN failure_incidents i ON i.id=n.incident_id;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') THEN
    GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO dbeaver_analyst;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grafana_reader') THEN
    GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO grafana_reader;
  END IF;
END $$;
''')


def downgrade():
    op.execute(r'''
DROP VIEW IF EXISTS reporting.failure_notifications;
DROP VIEW IF EXISTS reporting.failure_cluster_summary;
DROP VIEW IF EXISTS reporting.failure_intelligence;
DELETE FROM feature_flags WHERE name='failure_improvement_automation';
DROP INDEX IF EXISTS improvement_proposals_failure_cluster_idx;
ALTER TABLE improvement_proposals
  DROP COLUMN IF EXISTS selected_option,
  DROP COLUMN IF EXISTS failure_cluster_key;
DROP TABLE IF EXISTS failure_incident_notifications;
DROP TABLE IF EXISTS failure_incident_reviews;
DROP TABLE IF EXISTS failure_incidents;
ALTER TABLE agent_runs
  DROP COLUMN IF EXISTS planning_diagnostics,
  DROP COLUMN IF EXISTS failure_fingerprint,
  DROP COLUMN IF EXISTS intent_evidence,
  DROP COLUMN IF EXISTS intent_kind;
''')
