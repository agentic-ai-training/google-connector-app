"""Coalesced transactional Workspace wake hints; no approval authority in signals.

Revision ID: 029
Revises: 028
"""
from alembic import op

revision = '029'
down_revision = '028'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
ALTER TABLE temporal_dispatch_control ADD COLUMN notifications_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN notification_evidence_reference TEXT,
  ADD CHECK(NOT notifications_enabled OR (notification_evidence_reference IS NOT NULL
    AND length(notification_evidence_reference) BETWEEN 8 AND 256));
CREATE TABLE temporal_refresh_outbox (
  run_id UUID PRIMARY KEY,
  parent_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
  workflow_kind TEXT NOT NULL DEFAULT 'workspace' CHECK(workflow_kind='workspace'),
  FOREIGN KEY(workflow_kind,run_id) REFERENCES temporal_start_intents(workflow_kind,run_id),
  desired_generation BIGINT NOT NULL DEFAULT 1 CHECK(desired_generation BETWEEN 1 AND 1000000000),
  acknowledged_generation BIGINT NOT NULL DEFAULT 0,
  inflight_generation BIGINT,
  lease_token UUID,
  lease_expires_at TIMESTAMPTZ,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 10),
  blocked BOOLEAN NOT NULL DEFAULT FALSE,
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  error_category TEXT CHECK(error_category IS NULL OR error_category IN ('transport','binding','workflow_closed','retry_exhausted')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK(parent_id IS NULL OR parent_id=run_id),
  CHECK(acknowledged_generation BETWEEN 0 AND desired_generation),
  CHECK((lease_token IS NULL AND lease_expires_at IS NULL AND inflight_generation IS NULL)
    OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
        AND inflight_generation>acknowledged_generation AND inflight_generation<=desired_generation))
);
CREATE INDEX temporal_refresh_pending ON temporal_refresh_outbox(available_at)
  WHERE desired_generation>acknowledged_generation AND NOT blocked;
CREATE FUNCTION protect_temporal_refresh() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' THEN
    IF OLD.desired_generation>OLD.acknowledged_generation OR OLD.lease_token IS NOT NULL THEN
      RAISE EXCEPTION 'pending Temporal refresh cannot be discarded';
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.workflow_kind IS DISTINCT FROM OLD.workflow_kind
     OR (NEW.parent_id IS NOT NULL AND NEW.parent_id IS DISTINCT FROM OLD.parent_id)
     OR NEW.desired_generation<OLD.desired_generation
     OR NEW.acknowledged_generation<OLD.acknowledged_generation THEN
    RAISE EXCEPTION 'Temporal refresh identity and generation are monotone';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER temporal_refresh_binding BEFORE UPDATE OR DELETE ON temporal_refresh_outbox
  FOR EACH ROW EXECUTE FUNCTION protect_temporal_refresh();
CREATE FUNCTION capture_temporal_refresh() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source agent_runs%ROWTYPE; admitted BOOLEAN;
BEGIN
  source := CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
  IF source.orchestration_owner<>'temporal' THEN RETURN source; END IF;
  IF TG_OP='UPDATE' AND NOT (
    (NEW.status IS DISTINCT FROM OLD.status AND NEW.status IN
      ('queued','cancelled','awaiting_approval','awaiting_clarification'))
    OR NEW.plan IS DISTINCT FROM OLD.plan
    OR NEW.clarification_answers IS DISTINCT FROM OLD.clarification_answers
    OR (NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL)) THEN RETURN source; END IF;
  SELECT notifications_enabled INTO admitted FROM temporal_dispatch_control WHERE singleton FOR UPDATE;
  IF NOT admitted THEN RETURN source; END IF;
  IF NOT EXISTS(SELECT 1 FROM temporal_refresh_outbox WHERE run_id=source.id
        AND desired_generation>acknowledged_generation)
     AND (SELECT count(*) FROM temporal_refresh_outbox WHERE desired_generation>acknowledged_generation)>=2000 THEN
    RAISE EXCEPTION 'Temporal refresh capacity exhausted';
  END IF;
  INSERT INTO temporal_refresh_outbox(run_id,parent_id) VALUES(source.id,source.id)
  ON CONFLICT(run_id) DO UPDATE SET desired_generation=temporal_refresh_outbox.desired_generation+1,
    blocked=FALSE,error_category=NULL,available_at=now(),updated_at=now(),
    attempt_count=CASE WHEN temporal_refresh_outbox.lease_token IS NULL THEN 0
      ELSE temporal_refresh_outbox.attempt_count END;
  RETURN source;
END $$;
CREATE TRIGGER temporal_refresh_updated AFTER UPDATE ON agent_runs
  FOR EACH ROW EXECUTE FUNCTION capture_temporal_refresh();
CREATE TRIGGER temporal_refresh_deleted BEFORE DELETE ON agent_runs
  FOR EACH ROW EXECUTE FUNCTION capture_temporal_refresh();
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF (SELECT notifications_enabled FROM temporal_dispatch_control WHERE singleton)
    OR EXISTS(SELECT 1 FROM temporal_refresh_outbox WHERE desired_generation>acknowledged_generation OR lease_token IS NOT NULL) THEN
    RAISE EXCEPTION 'disable and reconcile Temporal refresh delivery before downgrade';
  END IF;
END $$;
DROP TRIGGER temporal_refresh_updated ON agent_runs;
DROP TRIGGER temporal_refresh_deleted ON agent_runs;
DROP FUNCTION capture_temporal_refresh();
DROP TABLE temporal_refresh_outbox;
DROP FUNCTION protect_temporal_refresh();
ALTER TABLE temporal_dispatch_control DROP COLUMN notifications_enabled,DROP COLUMN notification_evidence_reference;
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
