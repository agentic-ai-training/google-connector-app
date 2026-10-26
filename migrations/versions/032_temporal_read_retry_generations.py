"""Append read-only retry generations without resetting prior uncertainty.

Revision ID: 032
Revises: 031
"""
from alembic import op

revision = '032'
down_revision = '031'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
ALTER TABLE temporal_workspace_steps ADD COLUMN generation SMALLINT NOT NULL DEFAULT 0 CHECK(generation BETWEEN 0 AND 3);
ALTER TABLE temporal_workspace_steps ADD COLUMN execution_policy TEXT NOT NULL DEFAULT 'legacy-v1'
  CHECK(execution_policy IN ('legacy-v1','read-safe-v1'));
ALTER TABLE temporal_workspace_steps DROP CONSTRAINT temporal_workspace_steps_pkey;
ALTER TABLE temporal_workspace_steps ADD PRIMARY KEY(run_id,step_id,generation);
CREATE TABLE temporal_read_retry_permits (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL,
  step_id UUID NOT NULL,
  run_parent_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
  step_parent_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
  actor_tenant_id UUID REFERENCES workflow_event_tenants(tenant_id) ON DELETE SET NULL,
  generation SMALLINT NOT NULL CHECK(generation BETWEEN 1 AND 3),
  prior_token UUID NOT NULL,
  execution_token UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  source_hash TEXT NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
  issued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY(run_id,step_id,prior_token) REFERENCES temporal_workspace_steps(run_id,step_id,execution_token),
  CHECK(run_parent_id IS NULL OR run_parent_id=run_id),
  CHECK(step_parent_id IS NULL OR step_parent_id=step_id),
  CHECK(expires_at>issued_at AND expires_at<=issued_at+interval '15 minutes')
);
CREATE INDEX temporal_read_retry_lookup ON temporal_read_retry_permits(run_id,step_id,generation,issued_at DESC);
ALTER TABLE temporal_workspace_steps ADD COLUMN retry_permit_id UUID REFERENCES temporal_read_retry_permits(id);
ALTER TABLE temporal_workspace_steps ADD CONSTRAINT temporal_generation_permit CHECK(
  (generation=0 AND retry_permit_id IS NULL) OR (generation>0 AND retry_permit_id IS NOT NULL AND read_only));
CREATE FUNCTION protect_read_retry_permit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.id,NEW.run_id,NEW.step_id,NEW.generation,NEW.prior_token,NEW.execution_token,NEW.source_hash,NEW.issued_at,NEW.expires_at)
     IS DISTINCT FROM ROW(OLD.id,OLD.run_id,OLD.step_id,OLD.generation,OLD.prior_token,OLD.execution_token,OLD.source_hash,OLD.issued_at,OLD.expires_at)
     OR (NEW.run_parent_id IS NOT NULL AND NEW.run_parent_id IS DISTINCT FROM OLD.run_parent_id)
     OR (NEW.step_parent_id IS NOT NULL AND NEW.step_parent_id IS DISTINCT FROM OLD.step_parent_id)
     OR (NEW.actor_tenant_id IS NOT NULL AND NEW.actor_tenant_id IS DISTINCT FROM OLD.actor_tenant_id) THEN
    RAISE EXCEPTION 'read retry permit is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER read_retry_binding BEFORE UPDATE ON temporal_read_retry_permits
  FOR EACH ROW EXECUTE FUNCTION protect_read_retry_permit();
CREATE OR REPLACE FUNCTION protect_temporal_workspace_step() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.run_id,NEW.step_id,NEW.workflow_execution_id,NEW.activity_id,NEW.execution_token,NEW.read_only,
      NEW.generation,NEW.execution_policy,NEW.retry_permit_id)
    IS DISTINCT FROM ROW(OLD.run_id,OLD.step_id,OLD.workflow_execution_id,OLD.activity_id,OLD.execution_token,OLD.read_only,
      OLD.generation,OLD.execution_policy,OLD.retry_permit_id)
    OR (NEW.run_parent_id IS NOT NULL AND NEW.run_parent_id IS DISTINCT FROM OLD.run_parent_id)
    OR (NEW.step_parent_id IS NOT NULL AND NEW.step_parent_id IS DISTINCT FROM OLD.step_parent_id)
    OR (OLD.state='uncertain' AND NEW.state<>'uncertain') THEN
    RAISE EXCEPTION 'Temporal step identity and unresolved uncertainty cannot be overwritten';
  END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION admit_temporal_read_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p temporal_read_retry_permits; prior temporal_workspace_steps; s agent_run_steps; r agent_runs;
BEGIN
  IF NEW.generation=0 THEN RETURN NEW; END IF;
  SELECT * INTO p FROM temporal_read_retry_permits WHERE id=NEW.retry_permit_id;
  SELECT * INTO prior FROM temporal_workspace_steps WHERE run_id=NEW.run_id AND step_id=NEW.step_id
    ORDER BY generation DESC LIMIT 1;
  SELECT * INTO s FROM agent_run_steps WHERE id=NEW.step_id AND run_id=NEW.run_id;
  SELECT * INTO r FROM agent_runs WHERE id=NEW.run_id;
  IF p.id IS NULL OR prior.execution_token IS NULL OR s.id IS NULL OR r.id IS NULL OR NOT s.read_only
    OR p.run_id<>NEW.run_id OR p.step_id<>NEW.step_id OR p.generation<>NEW.generation
    OR prior.generation+1<>NEW.generation OR prior.execution_token<>p.prior_token
    OR NEW.execution_token<>p.execution_token OR NEW.workflow_execution_id<>prior.workflow_execution_id
    OR NOT prior.read_only OR prior.execution_policy<>'read-safe-v1' OR NEW.execution_policy<>'read-safe-v1'
    OR prior.state='executing' OR prior.completed_at IS NULL OR p.expires_at<=clock_timestamp()
    OR s.status<>'pending' OR s.attempt_count>=least(s.max_attempts,4) OR r.deleted_at IS NOT NULL
    OR r.orchestration_owner<>'temporal' OR r.status NOT IN ('queued','running')
    OR p.source_hash IS DISTINCT FROM temporal_resolution_source(NEW.run_id,NEW.step_id)
    OR NOT EXISTS(SELECT 1 FROM workflow_event_tenants t WHERE t.tenant_id=p.actor_tenant_id AND t.user_id=r.user_id)
    OR EXISTS(SELECT 1 FROM temporal_workspace_steps j WHERE j.run_id=NEW.run_id AND j.step_id=NEW.step_id
      AND (NOT j.read_only OR j.state='executing' OR j.completed_at IS NULL))
    OR EXISTS(SELECT 1 FROM agent_tool_receipts t WHERE t.run_id=NEW.run_id AND t.step_id=NEW.step_id AND NOT t.read_only) THEN
    RAISE EXCEPTION 'read retry generation is not admitted';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER temporal_read_generation_admission BEFORE INSERT ON temporal_workspace_steps
  FOR EACH ROW EXECUTE FUNCTION admit_temporal_read_generation();
CREATE FUNCTION temporal_read_superseded(identity UUID,step_identity UUID,token UUID) RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM temporal_read_retry_permits p JOIN temporal_workspace_steps j ON j.retry_permit_id=p.id
    JOIN temporal_workspace_steps prior ON prior.run_id=p.run_id AND prior.step_id=p.step_id AND prior.execution_token=p.prior_token
    WHERE p.run_id=identity AND p.step_id=step_identity AND p.prior_token=token
      AND prior.read_only AND prior.execution_policy='read-safe-v1' AND j.read_only AND j.execution_policy='read-safe-v1'
      AND j.execution_token=p.execution_token AND j.generation=p.generation)
$$;
CREATE OR REPLACE FUNCTION temporal_step_resolved(identity UUID,step_identity UUID) RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM temporal_step_resolutions p JOIN temporal_workspace_steps j USING(run_id,step_id,execution_token)
    WHERE p.run_id=identity AND p.step_id=step_identity AND p.expires_at>statement_timestamp()
      AND p.source_hash=temporal_resolution_source(identity,step_identity)
      AND j.generation=(SELECT max(generation) FROM temporal_workspace_steps WHERE run_id=identity AND step_id=step_identity))
$$;
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM temporal_read_retry_permits) OR EXISTS(SELECT 1 FROM temporal_workspace_steps WHERE generation>0) THEN
    RAISE EXCEPTION 'reconcile retained read retry generations before downgrade';
  END IF;
END $$;
DROP FUNCTION temporal_read_superseded(UUID,UUID,UUID);
DROP TRIGGER temporal_read_generation_admission ON temporal_workspace_steps;
DROP FUNCTION admit_temporal_read_generation();
ALTER TABLE temporal_workspace_steps DROP CONSTRAINT temporal_generation_permit;
ALTER TABLE temporal_workspace_steps DROP COLUMN retry_permit_id;
DROP TABLE temporal_read_retry_permits;
DROP FUNCTION protect_read_retry_permit();
ALTER TABLE temporal_workspace_steps DROP CONSTRAINT temporal_workspace_steps_pkey;
ALTER TABLE temporal_workspace_steps DROP COLUMN generation;
ALTER TABLE temporal_workspace_steps DROP COLUMN execution_policy;
ALTER TABLE temporal_workspace_steps ADD PRIMARY KEY(run_id,step_id);
CREATE OR REPLACE FUNCTION protect_temporal_workspace_step() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.run_id,NEW.step_id,NEW.workflow_execution_id,NEW.activity_id,NEW.execution_token,NEW.read_only)
    IS DISTINCT FROM ROW(OLD.run_id,OLD.step_id,OLD.workflow_execution_id,OLD.activity_id,OLD.execution_token,OLD.read_only)
    OR (NEW.run_parent_id IS NOT NULL AND NEW.run_parent_id IS DISTINCT FROM OLD.run_parent_id)
    OR (NEW.step_parent_id IS NOT NULL AND NEW.step_parent_id IS DISTINCT FROM OLD.step_parent_id)
    OR (OLD.state='uncertain' AND NEW.state<>'uncertain') THEN
    RAISE EXCEPTION 'Temporal step identity and unresolved uncertainty cannot be overwritten';
  END IF;
  RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION temporal_step_resolved(identity UUID,step_identity UUID) RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM temporal_step_resolutions p JOIN temporal_workspace_steps j USING(run_id,step_id,execution_token)
    WHERE p.run_id=identity AND p.step_id=step_identity AND p.expires_at>statement_timestamp()
      AND p.source_hash=temporal_resolution_source(identity,step_identity))
$$;
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
