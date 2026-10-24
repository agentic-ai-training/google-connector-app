"""Atomic reference-only Temporal start intents, independently disabled transport.

Revision ID: 027
Revises: 026
"""
from alembic import op

revision = '027'
down_revision = '026'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
CREATE TABLE temporal_dispatch_control (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  evidence_reference TEXT CHECK(length(evidence_reference) BETWEEN 8 AND 256),
  CHECK(NOT enabled OR evidence_reference IS NOT NULL)
);
INSERT INTO temporal_dispatch_control(singleton) VALUES(TRUE);
CREATE FUNCTION audit_temporal_dispatch_control() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
    INSERT INTO workflow_ownership_audit(workflow_kind,previous_policy,next_policy)
      VALUES('temporal_dispatch',to_jsonb(OLD),to_jsonb(NEW));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER temporal_dispatch_policy_audit AFTER UPDATE ON temporal_dispatch_control
  FOR EACH ROW EXECUTE FUNCTION audit_temporal_dispatch_control();
CREATE TABLE temporal_start_intents (
  workflow_kind TEXT NOT NULL CHECK(workflow_kind IN ('workspace','coding')),
  run_id UUID NOT NULL,
  workspace_parent_id UUID,
  coding_parent_id UUID,
  namespace TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  task_queue TEXT NOT NULL,
  workflow_version TEXT NOT NULL,
  executor_version TEXT NOT NULL,
  request_bytes BYTEA NOT NULL CHECK(octet_length(request_bytes) BETWEEN 1 AND 2048),
  request_sha256 TEXT NOT NULL CHECK(request_sha256=encode(sha256(request_bytes),'hex')),
  start_deadline TIMESTAMPTZ NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','claimed','uncertain','started','blocked')),
  may_have_started BOOLEAN NOT NULL DEFAULT FALSE,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 10),
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_token UUID,
  lease_expires_at TIMESTAMPTZ,
  first_execution_run_id UUID,
  last_error_category TEXT CHECK(last_error_category IS NULL OR last_error_category IN
    ('transport','binding','not_admissible','retry_exhausted')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(workflow_kind,run_id),
  UNIQUE(namespace,workflow_id),
  FOREIGN KEY(workspace_parent_id) REFERENCES agent_runs(id) ON DELETE SET NULL,
  FOREIGN KEY(coding_parent_id) REFERENCES coding_runs(id) ON DELETE SET NULL,
  CHECK((workflow_kind='workspace' AND coding_parent_id IS NULL
      AND (workspace_parent_id IS NULL OR workspace_parent_id=run_id))
    OR (workflow_kind='coding' AND workspace_parent_id IS NULL
      AND (coding_parent_id IS NULL OR coding_parent_id=run_id))),
  CHECK((state='claimed')=(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
  CHECK((state='started')=(first_execution_run_id IS NOT NULL))
);
CREATE INDEX temporal_start_claim ON temporal_start_intents(available_at,created_at)
  WHERE state IN ('pending','claimed','uncertain');

CREATE FUNCTION capture_temporal_start_intent() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind TEXT := TG_ARGV[0]; body BYTEA; expiry TIMESTAMPTZ;
BEGIN
  IF NEW.orchestration_owner<>'temporal' THEN RETURN NEW; END IF;
  -- Shared bounded admission, serialized before counting. Not a second executor.
  PERFORM singleton FROM temporal_dispatch_control WHERE singleton FOR UPDATE;
  IF (SELECT count(*) FROM temporal_start_intents WHERE state<>'started')>=2000 THEN
    RAISE EXCEPTION 'Temporal start intent capacity exhausted';
  END IF;
  SELECT expires_at INTO expiry FROM workflow_ownership_control WHERE workflow_kind=kind;
  body := convert_to(jsonb_build_object('schema','gca.temporal.start.v1','kind',kind,
    'run_id',NEW.id,'workflow_version',NEW.temporal_workflow_version,
    'executor_version',NEW.executor_version)::text,'UTF8');
  INSERT INTO temporal_start_intents(workflow_kind,run_id,workspace_parent_id,coding_parent_id,namespace,workflow_id,task_queue,
    workflow_version,executor_version,request_bytes,request_sha256,start_deadline)
    VALUES(kind,NEW.id,CASE WHEN kind='workspace' THEN NEW.id END,CASE WHEN kind='coding' THEN NEW.id END,
      NEW.temporal_namespace,NEW.temporal_workflow_id,NEW.temporal_task_queue,
      NEW.temporal_workflow_version,NEW.executor_version,body,encode(sha256(body),'hex'),
      LEAST(expiry,clock_timestamp()+interval '10 minutes'));
  RETURN NEW;
END $$;
CREATE TRIGGER temporal_workspace_start AFTER INSERT ON agent_runs
  FOR EACH ROW EXECUTE FUNCTION capture_temporal_start_intent('workspace');
CREATE TRIGGER temporal_coding_start AFTER INSERT ON coding_runs
  FOR EACH ROW EXECUTE FUNCTION capture_temporal_start_intent('coding');

CREATE FUNCTION protect_temporal_start_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.workflow_kind,NEW.run_id,NEW.namespace,NEW.workflow_id,NEW.task_queue,
     NEW.workflow_version,NEW.executor_version,NEW.request_bytes,NEW.request_sha256,NEW.start_deadline)
     IS DISTINCT FROM ROW(OLD.workflow_kind,OLD.run_id,OLD.namespace,OLD.workflow_id,OLD.task_queue,
     OLD.workflow_version,OLD.executor_version,OLD.request_bytes,OLD.request_sha256,OLD.start_deadline)
     OR (OLD.may_have_started AND NOT NEW.may_have_started)
     OR OLD.attempt_count>NEW.attempt_count
     OR (NEW.workspace_parent_id IS NOT NULL AND NEW.workspace_parent_id IS DISTINCT FROM OLD.workspace_parent_id)
     OR (NEW.coding_parent_id IS NOT NULL AND NEW.coding_parent_id IS DISTINCT FROM OLD.coding_parent_id)
     OR (OLD.first_execution_run_id IS NOT NULL AND
         NEW.first_execution_run_id IS DISTINCT FROM OLD.first_execution_run_id) THEN
    RAISE EXCEPTION 'Temporal start identity and uncertainty history are immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER temporal_start_binding BEFORE UPDATE ON temporal_start_intents
  FOR EACH ROW EXECUTE FUNCTION protect_temporal_start_binding();
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF (SELECT enabled FROM temporal_dispatch_control WHERE singleton)
     OR EXISTS(SELECT 1 FROM temporal_start_intents) THEN
    RAISE EXCEPTION 'disable transport and reconcile retained Temporal start intents before downgrade';
  END IF;
END $$;
DROP TRIGGER temporal_workspace_start ON agent_runs;
DROP TRIGGER temporal_coding_start ON coding_runs;
DROP FUNCTION capture_temporal_start_intent();
DROP TABLE temporal_start_intents;
DROP FUNCTION protect_temporal_start_binding();
DROP TABLE temporal_dispatch_control;
DROP FUNCTION audit_temporal_dispatch_control();
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
