"""Pin business runs to one immutable orchestration owner; no Temporal activation.

Revision ID: 026
Revises: 025
"""
from alembic import op

revision = '026'
down_revision = '025'
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
CREATE TABLE workflow_ownership_control (
  workflow_kind TEXT PRIMARY KEY CHECK(workflow_kind IN ('workspace','coding')),
  admission_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  namespace TEXT CHECK(namespace ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'),
  task_queue TEXT CHECK(task_queue ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'),
  workflow_version TEXT CHECK(workflow_version ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'),
  executor_version TEXT,
  evidence_reference TEXT CHECK(length(evidence_reference) BETWEEN 8 AND 256),
  expires_at TIMESTAMPTZ,
  CHECK(NOT admission_enabled OR (namespace IS NOT NULL AND task_queue IS NOT NULL
    AND workflow_version IS NOT NULL AND executor_version IS NOT NULL
    AND evidence_reference IS NOT NULL AND expires_at IS NOT NULL))
);
INSERT INTO workflow_ownership_control(workflow_kind) VALUES('workspace'),('coding');

CREATE TABLE IF NOT EXISTS workflow_ownership_audit (
  id BIGSERIAL PRIMARY KEY,
  workflow_kind TEXT NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  database_actor TEXT NOT NULL DEFAULT current_user,
  previous_policy JSONB NOT NULL,
  next_policy JSONB NOT NULL
);
CREATE FUNCTION audit_workflow_ownership_control() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
    INSERT INTO workflow_ownership_audit(workflow_kind,previous_policy,next_policy)
      VALUES(NEW.workflow_kind,to_jsonb(OLD),to_jsonb(NEW));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER workflow_ownership_policy_audit AFTER UPDATE ON workflow_ownership_control
  FOR EACH ROW EXECUTE FUNCTION audit_workflow_ownership_control();

CREATE FUNCTION enforce_workflow_ownership() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind TEXT := TG_ARGV[0]; control workflow_ownership_control%ROWTYPE;
BEGIN
  IF TG_OP='UPDATE' THEN
    IF ROW(NEW.orchestration_owner,NEW.temporal_namespace,NEW.temporal_workflow_id,
           NEW.temporal_task_queue,NEW.temporal_workflow_version)
       IS DISTINCT FROM
       ROW(OLD.orchestration_owner,OLD.temporal_namespace,OLD.temporal_workflow_id,
           OLD.temporal_task_queue,OLD.temporal_workflow_version) THEN
      RAISE EXCEPTION 'workflow ownership is immutable; reconcile rather than transfer live runs';
    END IF;
    IF OLD.orchestration_owner='temporal' AND
       ROW(NEW.id,NEW.user_id,NEW.executor_version) IS DISTINCT FROM
       ROW(OLD.id,OLD.user_id,OLD.executor_version) THEN
      RAISE EXCEPTION 'Temporal run identity and executor version are pinned';
    END IF;
  ELSIF NEW.orchestration_owner='temporal' THEN
    SELECT * INTO control FROM workflow_ownership_control WHERE workflow_kind=kind FOR SHARE;
    IF NOT control.admission_enabled OR control.expires_at<=clock_timestamp()
       OR control.expires_at>clock_timestamp()+interval '24 hours'
       OR ROW(NEW.temporal_namespace,NEW.temporal_task_queue,NEW.temporal_workflow_version,NEW.executor_version)
          IS DISTINCT FROM ROW(control.namespace,control.task_queue,control.workflow_version,control.executor_version)
       OR NEW.status<>'queued' OR NEW.started_at IS NOT NULL
       OR NEW.heartbeat_at IS NOT NULL OR NEW.deleted_at IS NOT NULL
       OR (kind='coding' AND COALESCE((to_jsonb(NEW)->>'attempt_count')::integer,0)<>0) THEN
      RAISE EXCEPTION 'Temporal admission requires current reviewed scope and a fresh queued run';
    END IF;
  END IF;
  IF NEW.orchestration_owner='temporal' THEN
    IF NEW.temporal_workflow_id IS DISTINCT FROM ('gca.'||kind||'.'||NEW.id::text)
       OR NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL THEN
      RAISE EXCEPTION 'Temporal runs cannot receive PostgreSQL worker leases or another workflow ID';
    END IF;
  END IF;
  RETURN NEW;
END $$;

DO $$ DECLARE tab TEXT; kind TEXT;
BEGIN
  FOREACH tab IN ARRAY ARRAY['agent_runs','coding_runs'] LOOP
    kind := CASE WHEN tab='agent_runs' THEN 'workspace' ELSE 'coding' END;
    EXECUTE format('ALTER TABLE %I
      ADD COLUMN orchestration_owner TEXT NOT NULL DEFAULT ''postgres''
        CHECK(orchestration_owner IN (''postgres'',''temporal'')),
      ADD COLUMN temporal_namespace TEXT,
      ADD COLUMN temporal_workflow_id TEXT,
      ADD COLUMN temporal_task_queue TEXT,
      ADD COLUMN temporal_workflow_version TEXT,
      ADD CONSTRAINT %I CHECK(
        (orchestration_owner=''postgres'' AND temporal_namespace IS NULL
          AND temporal_workflow_id IS NULL AND temporal_task_queue IS NULL AND temporal_workflow_version IS NULL)
        OR (orchestration_owner=''temporal'' AND temporal_namespace IS NOT NULL
          AND temporal_workflow_id IS NOT NULL AND temporal_task_queue IS NOT NULL
          AND temporal_workflow_version IS NOT NULL))',tab,tab||'_workflow_binding');
    EXECUTE format('CREATE UNIQUE INDEX %I ON %I(temporal_namespace,temporal_workflow_id)
      WHERE orchestration_owner=''temporal''',tab||'_temporal_identity',tab);
    EXECUTE format('CREATE TRIGGER workflow_owner_guard BEFORE INSERT OR UPDATE ON %I
      FOR EACH ROW EXECUTE FUNCTION enforce_workflow_ownership(%L)',tab,kind);
  END LOOP;
END $$;
"""

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM workflow_ownership_control WHERE admission_enabled)
     OR EXISTS(SELECT 1 FROM agent_runs WHERE orchestration_owner='temporal')
     OR EXISTS(SELECT 1 FROM coding_runs WHERE orchestration_owner='temporal') THEN
    RAISE EXCEPTION 'disable Temporal admission and reconcile retained Temporal-owned runs before downgrade';
  END IF;
END $$;
DROP TRIGGER workflow_owner_guard ON agent_runs;
DROP TRIGGER workflow_owner_guard ON coding_runs;
DROP FUNCTION enforce_workflow_ownership();
ALTER TABLE agent_runs DROP COLUMN orchestration_owner,DROP COLUMN temporal_namespace,
  DROP COLUMN temporal_workflow_id,DROP COLUMN temporal_task_queue,DROP COLUMN temporal_workflow_version;
ALTER TABLE coding_runs DROP COLUMN orchestration_owner,DROP COLUMN temporal_namespace,
  DROP COLUMN temporal_workflow_id,DROP COLUMN temporal_task_queue,DROP COLUMN temporal_workflow_version;
DROP TRIGGER workflow_ownership_policy_audit ON workflow_ownership_control;
DROP FUNCTION audit_workflow_ownership_control();
-- Audit facts survive rollback; an idempotent re-upgrade preserves them.
DROP TABLE workflow_ownership_control;
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
