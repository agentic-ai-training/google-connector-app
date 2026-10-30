"""Preserve uncertainty and append source-bound verified readback resolutions.

Revision ID: 030
Revises: 029
"""
from alembic import op

revision = '030'
down_revision = '029'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
ALTER TABLE temporal_workspace_steps ADD CONSTRAINT temporal_journal_token UNIQUE(run_id,step_id,execution_token);
CREATE FUNCTION temporal_resolution_source(identity UUID,step_identity UUID) RETURNS TEXT LANGUAGE SQL STABLE AS $$
  SELECT encode(sha256(convert_to(jsonb_build_object(
    'run_id',r.id,'user_id',r.user_id,'plan',r.plan,'executor',r.executor_version,
    'run_risk',r.risk_level,'run_requires_approval',r.requires_approval,'approval_bypassed',r.approval_bypassed,
    'step_id',s.id,'service',s.service,'operation',s.operation,'read_only',s.read_only,
    'requires_approval',s.requires_approval,'risk_level',s.risk_level,
    'preconditions',s.preconditions,'postconditions',s.postconditions,
    'dependencies',s.dependencies,'attempt_count',s.attempt_count,
    'input',s.input_data,'output',s.output_data,
    'artifacts',COALESCE((SELECT jsonb_agg(jsonb_build_object(
      'id',a.id,'external_id',a.external_id,'url',a.url,'verification',a.verification_status,
      'cleanup',a.cleanup_state,'sharing',a.sharing_state,'metadata',a.metadata) ORDER BY a.id)
      FROM agent_artifacts a WHERE a.run_id=r.id AND a.step_id=s.id),'[]'::jsonb)
    )::text,'UTF8')),'hex')
  FROM agent_runs r JOIN agent_run_steps s ON s.run_id=r.id
  WHERE r.id=identity AND s.id=step_identity AND r.deleted_at IS NULL
    AND r.orchestration_owner='temporal'
$$;
CREATE TABLE temporal_step_resolutions (
  run_id UUID NOT NULL,
  step_id UUID NOT NULL,
  execution_token UUID NOT NULL,
  source_hash TEXT NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
  verification_hash TEXT NOT NULL CHECK(verification_hash ~ '^[0-9a-f]{64}$'),
  actor_tenant_id UUID REFERENCES workflow_event_tenants(tenant_id) ON DELETE SET NULL,
  verification_started_at TIMESTAMPTZ NOT NULL,
  verified_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(run_id,step_id,execution_token,source_hash,verification_started_at),
  FOREIGN KEY(run_id,step_id,execution_token) REFERENCES temporal_workspace_steps(run_id,step_id,execution_token),
  CHECK(verification_started_at<=verified_at AND expires_at>verified_at
    AND expires_at<=verification_started_at+interval '15 minutes')
);
CREATE FUNCTION protect_temporal_resolution() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.run_id,NEW.step_id,NEW.execution_token,NEW.source_hash,NEW.verification_hash,
      NEW.verification_started_at,NEW.verified_at,NEW.expires_at)
     IS DISTINCT FROM ROW(OLD.run_id,OLD.step_id,OLD.execution_token,OLD.source_hash,OLD.verification_hash,
      OLD.verification_started_at,OLD.verified_at,OLD.expires_at)
     OR (NEW.actor_tenant_id IS NOT NULL AND NEW.actor_tenant_id IS DISTINCT FROM OLD.actor_tenant_id) THEN
    RAISE EXCEPTION 'verified Temporal resolution is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER temporal_resolution_binding BEFORE UPDATE ON temporal_step_resolutions
  FOR EACH ROW EXECUTE FUNCTION protect_temporal_resolution();
CREATE FUNCTION temporal_step_resolved(identity UUID,step_identity UUID) RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM temporal_step_resolutions p JOIN temporal_workspace_steps j
    USING(run_id,step_id,execution_token)
    WHERE p.run_id=identity AND p.step_id=step_identity AND p.expires_at>statement_timestamp()
      AND p.source_hash=temporal_resolution_source(identity,step_identity))
$$;
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM temporal_step_resolutions) THEN
    RAISE EXCEPTION 'reconcile retained Temporal resolution evidence before downgrade';
  END IF;
END $$;
DROP FUNCTION temporal_step_resolved(UUID,UUID);
DROP TABLE temporal_step_resolutions;
DROP FUNCTION protect_temporal_resolution();
DROP FUNCTION temporal_resolution_source(UUID,UUID);
ALTER TABLE temporal_workspace_steps DROP CONSTRAINT temporal_journal_token;
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
