"""Admit verified-prefix write continuations without weakening read retries.

Revision ID: 034
Revises: 033
"""
from alembic import op

revision = '034'
down_revision = '033'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
ALTER TABLE tool_prefix_continuations DROP CONSTRAINT tool_prefix_continuations_run_id_step_id_next_attempt_key;
CREATE INDEX tool_prefix_attempt_lookup ON tool_prefix_continuations(run_id,step_id,next_attempt,created_at DESC);
CREATE TABLE temporal_prefix_generations (
  id UUID PRIMARY KEY,
  run_id UUID NOT NULL,
  step_id UUID NOT NULL,
  run_parent_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
  step_parent_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
  actor_tenant_id UUID REFERENCES workflow_event_tenants(tenant_id) ON DELETE SET NULL,
  prefix_grant_id UUID UNIQUE REFERENCES tool_prefix_continuations(id) ON DELETE SET NULL,
  generation SMALLINT NOT NULL CHECK(generation BETWEEN 1 AND 3),
  prior_token UUID NOT NULL,
  execution_token UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  workflow_execution_id UUID NOT NULL,
  source_hash TEXT NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
  verification_hash TEXT NOT NULL CHECK(verification_hash ~ '^[0-9a-f]{64}$'),
  verification_started_at TIMESTAMPTZ NOT NULL,
  issued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY(run_id,step_id,prior_token) REFERENCES temporal_workspace_steps(run_id,step_id,execution_token),
  CHECK(run_parent_id IS NULL OR run_parent_id=run_id),
  CHECK(step_parent_id IS NULL OR step_parent_id=step_id),
  CHECK(prefix_grant_id IS NULL OR prefix_grant_id=id),
  CHECK(verification_started_at<=issued_at AND expires_at>issued_at
    AND expires_at<=verification_started_at+interval '15 minutes')
);
CREATE INDEX temporal_prefix_generation_lookup ON temporal_prefix_generations(run_id,step_id,generation,issued_at DESC);
REVOKE ALL ON temporal_prefix_generations FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') THEN
    REVOKE ALL ON temporal_prefix_generations FROM dbeaver_analyst;
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='grafana_reader') THEN
    REVOKE ALL ON temporal_prefix_generations FROM grafana_reader;
  END IF;
END $$;
ALTER TABLE temporal_workspace_steps ADD COLUMN prefix_permit_id UUID REFERENCES temporal_prefix_generations(id);
ALTER TABLE temporal_workspace_steps DROP CONSTRAINT temporal_workspace_steps_execution_policy_check;
ALTER TABLE temporal_workspace_steps ADD CONSTRAINT temporal_workspace_steps_execution_policy_check
  CHECK(execution_policy IN ('legacy-v1','read-safe-v1','prefix-safe-v1'));
ALTER TABLE temporal_workspace_steps DROP CONSTRAINT temporal_generation_permit;
ALTER TABLE temporal_workspace_steps ADD CONSTRAINT temporal_generation_permit CHECK(
  (generation=0 AND retry_permit_id IS NULL AND prefix_permit_id IS NULL) OR
  (generation>0 AND read_only AND retry_permit_id IS NOT NULL AND prefix_permit_id IS NULL) OR
  (generation>0 AND NOT read_only AND retry_permit_id IS NULL AND prefix_permit_id IS NOT NULL AND execution_policy='prefix-safe-v1'));
CREATE FUNCTION protect_temporal_prefix_generation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.id,NEW.run_id,NEW.step_id,NEW.generation,NEW.prior_token,NEW.execution_token,NEW.workflow_execution_id,
      NEW.source_hash,NEW.verification_hash,NEW.verification_started_at,NEW.issued_at,NEW.expires_at)
    IS DISTINCT FROM ROW(OLD.id,OLD.run_id,OLD.step_id,OLD.generation,OLD.prior_token,OLD.execution_token,OLD.workflow_execution_id,
      OLD.source_hash,OLD.verification_hash,OLD.verification_started_at,OLD.issued_at,OLD.expires_at)
    OR (NEW.run_parent_id IS NOT NULL AND NEW.run_parent_id IS DISTINCT FROM OLD.run_parent_id)
    OR (NEW.step_parent_id IS NOT NULL AND NEW.step_parent_id IS DISTINCT FROM OLD.step_parent_id)
    OR (NEW.actor_tenant_id IS NOT NULL AND NEW.actor_tenant_id IS DISTINCT FROM OLD.actor_tenant_id)
    OR (NEW.prefix_grant_id IS NOT NULL AND NEW.prefix_grant_id IS DISTINCT FROM OLD.prefix_grant_id) THEN
    RAISE EXCEPTION 'Temporal prefix generation is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER temporal_prefix_generation_binding BEFORE UPDATE ON temporal_prefix_generations
  FOR EACH ROW EXECUTE FUNCTION protect_temporal_prefix_generation();
CREATE OR REPLACE FUNCTION protect_temporal_workspace_step() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.run_id,NEW.step_id,NEW.workflow_execution_id,NEW.activity_id,NEW.execution_token,NEW.read_only,
      NEW.generation,NEW.execution_policy,NEW.retry_permit_id,NEW.prefix_permit_id)
    IS DISTINCT FROM ROW(OLD.run_id,OLD.step_id,OLD.workflow_execution_id,OLD.activity_id,OLD.execution_token,OLD.read_only,
      OLD.generation,OLD.execution_policy,OLD.retry_permit_id,OLD.prefix_permit_id)
    OR (NEW.run_parent_id IS NOT NULL AND NEW.run_parent_id IS DISTINCT FROM OLD.run_parent_id)
    OR (NEW.step_parent_id IS NOT NULL AND NEW.step_parent_id IS DISTINCT FROM OLD.step_parent_id)
    OR (OLD.state='uncertain' AND NEW.state<>'uncertain') THEN
    RAISE EXCEPTION 'Temporal step identity and unresolved uncertainty cannot be overwritten';
  END IF;
  RETURN NEW;
END $$;
-- Keep the complete schema-032 read guard; a separate BEFORE trigger independently
-- checks every prefix generation. Neither branch can masquerade as the other.
ALTER FUNCTION admit_temporal_read_generation() RENAME TO admit_temporal_read_generation_v032;
DROP TRIGGER temporal_read_generation_admission ON temporal_workspace_steps;
CREATE FUNCTION admit_temporal_prefix_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p temporal_prefix_generations; g tool_prefix_continuations; prior temporal_workspace_steps;
  s agent_run_steps; r agent_runs;
BEGIN
  IF NEW.prefix_permit_id IS NULL THEN RETURN NEW; END IF;
  SELECT * INTO p FROM temporal_prefix_generations WHERE id=NEW.prefix_permit_id;
  SELECT * INTO g FROM tool_prefix_continuations WHERE id=p.prefix_grant_id;
  SELECT * INTO prior FROM temporal_workspace_steps WHERE run_id=NEW.run_id AND step_id=NEW.step_id
    ORDER BY generation DESC LIMIT 1;
  SELECT * INTO s FROM agent_run_steps WHERE id=NEW.step_id AND run_id=NEW.run_id;
  SELECT * INTO r FROM agent_runs WHERE id=NEW.run_id;
  IF p.id IS NULL OR g.id IS NULL OR prior.execution_token IS NULL OR s.id IS NULL OR r.id IS NULL
    OR NEW.generation=0 OR s.read_only OR NEW.read_only OR NEW.retry_permit_id IS NOT NULL
    OR p.run_id<>NEW.run_id OR p.step_id<>NEW.step_id OR p.generation<>NEW.generation
    OR g.run_id<>NEW.run_id OR g.step_id<>NEW.step_id OR g.next_attempt<>s.attempt_count+1
    OR s.attempt_count<>NEW.generation OR prior.generation+1<>NEW.generation
    OR prior.execution_token<>p.prior_token OR NEW.execution_token<>p.execution_token
    OR NEW.workflow_execution_id<>prior.workflow_execution_id OR p.workflow_execution_id<>NEW.workflow_execution_id
    OR prior.read_only OR prior.execution_policy<>'prefix-safe-v1' OR NEW.execution_policy<>'prefix-safe-v1'
    OR prior.state='executing' OR prior.completed_at IS NULL OR p.expires_at<=clock_timestamp()
    OR g.consumed_at IS NOT NULL OR g.expires_at<=clock_timestamp() OR g.executor_version<>r.executor_version
    OR s.status<>'pending' OR r.deleted_at IS NOT NULL OR r.orchestration_owner<>'temporal'
    OR r.status NOT IN ('queued','running')
    OR NEW.activity_id<>('step.'||NEW.step_id::text||'.g'||NEW.generation::text||'.'||p.id::text)
    OR p.source_hash IS DISTINCT FROM temporal_resolution_source(NEW.run_id,NEW.step_id)
    OR NOT EXISTS(SELECT 1 FROM workflow_event_tenants t WHERE t.tenant_id=p.actor_tenant_id AND t.user_id=r.user_id)
    OR NOT EXISTS(SELECT 1 FROM temporal_start_intents t WHERE t.run_id=NEW.run_id AND t.workflow_kind='workspace'
      AND t.state='started' AND t.first_execution_run_id=NEW.workflow_execution_id)
    OR EXISTS(SELECT 1 FROM temporal_workspace_steps j WHERE j.run_id=NEW.run_id AND j.step_id=NEW.step_id
      AND (j.read_only OR j.execution_policy<>'prefix-safe-v1' OR j.state='executing' OR j.completed_at IS NULL))
    OR (SELECT count(*) FROM agent_tool_receipts t WHERE t.run_id=NEW.run_id AND t.step_id=NEW.step_id)<>g.prefix_length
    OR EXISTS(SELECT 1 FROM agent_tool_receipts t WHERE t.run_id=NEW.run_id AND t.step_id=NEW.step_id
      AND (t.state<>'returned' OR t.encrypted_payload IS NULL OR t.expires_at<=clock_timestamp())) THEN
    RAISE EXCEPTION 'verified write prefix generation is not admitted';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER temporal_prefix_generation_admission BEFORE INSERT ON temporal_workspace_steps
  FOR EACH ROW EXECUTE FUNCTION admit_temporal_prefix_generation();
-- A WHEN expression preserves the original read-only trigger byte-for-byte.
CREATE TRIGGER temporal_read_generation_admission BEFORE INSERT ON temporal_workspace_steps
  FOR EACH ROW WHEN (NEW.prefix_permit_id IS NULL) EXECUTE FUNCTION admit_temporal_read_generation_v032();
CREATE FUNCTION temporal_prefix_superseded(identity UUID,step_identity UUID,token UUID) RETURNS BOOLEAN LANGUAGE SQL STABLE AS $$
  SELECT EXISTS(SELECT 1 FROM temporal_prefix_generations p
    JOIN temporal_workspace_steps j ON j.prefix_permit_id=p.id
    JOIN temporal_workspace_steps prior ON prior.run_id=p.run_id AND prior.step_id=p.step_id AND prior.execution_token=p.prior_token
    WHERE p.run_id=identity AND p.step_id=step_identity AND p.prior_token=token
      AND NOT prior.read_only AND prior.execution_policy='prefix-safe-v1' AND prior.completed_at IS NOT NULL
      AND NOT j.read_only AND j.execution_policy='prefix-safe-v1'
      AND j.execution_token=p.execution_token AND j.generation=p.generation)
$$;
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM temporal_prefix_generations)
    OR EXISTS(SELECT 1 FROM temporal_workspace_steps WHERE execution_policy='prefix-safe-v1')
    OR EXISTS(SELECT 1 FROM tool_prefix_continuations GROUP BY run_id,step_id,next_attempt HAVING count(*)>1) THEN
    RAISE EXCEPTION 'retain verified prefix generations before downgrade';
  END IF;
END $$;
DROP INDEX tool_prefix_attempt_lookup;
ALTER TABLE tool_prefix_continuations ADD UNIQUE(run_id,step_id,next_attempt);
DROP FUNCTION temporal_prefix_superseded(UUID,UUID,UUID);
DROP TRIGGER temporal_prefix_generation_admission ON temporal_workspace_steps;
DROP FUNCTION admit_temporal_prefix_generation();
DROP TRIGGER temporal_read_generation_admission ON temporal_workspace_steps;
ALTER FUNCTION admit_temporal_read_generation_v032() RENAME TO admit_temporal_read_generation;
CREATE TRIGGER temporal_read_generation_admission BEFORE INSERT ON temporal_workspace_steps
  FOR EACH ROW EXECUTE FUNCTION admit_temporal_read_generation();
ALTER TABLE temporal_workspace_steps DROP CONSTRAINT temporal_generation_permit;
ALTER TABLE temporal_workspace_steps DROP COLUMN prefix_permit_id;
ALTER TABLE temporal_workspace_steps DROP CONSTRAINT temporal_workspace_steps_execution_policy_check;
ALTER TABLE temporal_workspace_steps ADD CONSTRAINT temporal_workspace_steps_execution_policy_check
  CHECK(execution_policy IN ('legacy-v1','read-safe-v1'));
ALTER TABLE temporal_workspace_steps ADD CONSTRAINT temporal_generation_permit CHECK(
  (generation=0 AND retry_permit_id IS NULL) OR (generation>0 AND retry_permit_id IS NOT NULL AND read_only));
DROP TABLE temporal_prefix_generations;
DROP FUNCTION protect_temporal_prefix_generation();
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
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
