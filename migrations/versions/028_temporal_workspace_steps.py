"""Write-ahead journal for actual Workspace activities, with erasure-safe references.

Revision ID: 028
Revises: 027
"""
from alembic import op

revision = '028'
down_revision = '027'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
CREATE TABLE temporal_workspace_steps (
  run_id UUID NOT NULL,
  step_id UUID NOT NULL,
  run_parent_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
  step_parent_id UUID REFERENCES agent_run_steps(id) ON DELETE SET NULL,
  workflow_execution_id UUID NOT NULL,
  activity_id TEXT NOT NULL CHECK(length(activity_id) BETWEEN 1 AND 200),
  execution_token UUID NOT NULL DEFAULT gen_random_uuid(),
  read_only BOOLEAN NOT NULL,
  state TEXT NOT NULL DEFAULT 'executing' CHECK(state IN ('executing','succeeded','failed','uncertain')),
  error_category TEXT CHECK(error_category IS NULL OR error_category IN ('execution','worker_reconciliation','cancelled')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  completed_at TIMESTAMPTZ,
  PRIMARY KEY(run_id,step_id),
  CHECK(run_parent_id IS NULL OR run_parent_id=run_id),
  CHECK(step_parent_id IS NULL OR step_parent_id=step_id)
);
CREATE FUNCTION protect_temporal_workspace_step() RETURNS trigger LANGUAGE plpgsql AS $$
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
CREATE TRIGGER temporal_workspace_step_binding BEFORE UPDATE ON temporal_workspace_steps
  FOR EACH ROW EXECUTE FUNCTION protect_temporal_workspace_step();
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM temporal_workspace_steps) THEN
    RAISE EXCEPTION 'reconcile retained Temporal activity history before downgrade';
  END IF;
END $$;
DROP TABLE temporal_workspace_steps;
DROP FUNCTION protect_temporal_workspace_step();
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
