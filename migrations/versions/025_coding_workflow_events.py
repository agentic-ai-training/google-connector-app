"""Add separately gated coding events to the same transactional delivery stream.

Revision ID: 025
Revises: 024
"""
from alembic import op

revision = '025'
down_revision = '024'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
ALTER TABLE coding_runs ADD COLUMN workflow_event_sequence BIGINT NOT NULL DEFAULT 0 CHECK(workflow_event_sequence>=0);
ALTER TABLE workflow_delivery_control ADD COLUMN capture_coding_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE workflow_delivery_audit ADD COLUMN workflow_kind TEXT NOT NULL DEFAULT 'workspace'
  CHECK(workflow_kind IN ('workspace','coding'));

ALTER TABLE workflow_event_envelopes
  DROP CONSTRAINT workflow_event_envelopes_run_id_fkey,
  DROP CONSTRAINT workflow_event_envelopes_source_event_id_key,
  DROP CONSTRAINT workflow_event_envelopes_run_id_sequence_no_key,
  ADD COLUMN workflow_kind TEXT NOT NULL DEFAULT 'workspace' CHECK(workflow_kind IN ('workspace','coding')),
  ADD COLUMN workspace_parent_id UUID GENERATED ALWAYS AS
    (CASE WHEN workflow_kind='workspace' THEN run_id ELSE NULL END) STORED,
  ADD COLUMN coding_parent_id UUID GENERATED ALWAYS AS
    (CASE WHEN workflow_kind='coding' THEN run_id ELSE NULL END) STORED,
  ADD CONSTRAINT workflow_workspace_parent FOREIGN KEY(workspace_parent_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
  ADD CONSTRAINT workflow_coding_parent FOREIGN KEY(coding_parent_id) REFERENCES coding_runs(id) ON DELETE CASCADE,
  ADD CONSTRAINT workflow_source_identity UNIQUE(workflow_kind,source_event_id),
  ADD CONSTRAINT workflow_run_sequence UNIQUE(workflow_kind,run_id,sequence_no);

CREATE TABLE workflow_coding_event_projection (
  run_id UUID PRIMARY KEY REFERENCES coding_runs(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL,
  last_event_id UUID NOT NULL,
  last_sequence BIGINT NOT NULL CHECK(last_sequence>0),
  observed_event_count BIGINT NOT NULL DEFAULT 1 CHECK(observed_event_count>0),
  last_event_type TEXT NOT NULL,
  last_phase TEXT,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY(last_event_id,tenant_id) REFERENCES workflow_event_envelopes(event_id,tenant_id) ON DELETE CASCADE
);
CREATE INDEX workflow_coding_projection_tenant ON workflow_coding_event_projection(tenant_id,observed_at);

CREATE FUNCTION capture_coding_workflow_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE seq BIGINT; tenant UUID; identity UUID := gen_random_uuid(); body BYTEA;
BEGIN
  IF NOT (SELECT capture_coding_enabled FROM workflow_delivery_control WHERE singleton) THEN RETURN NEW; END IF;
  UPDATE coding_runs SET workflow_event_sequence=workflow_event_sequence+1
    WHERE id=NEW.run_id AND user_id=NEW.user_id AND deleted_at IS NULL RETURNING workflow_event_sequence INTO seq;
  IF seq IS NULL THEN RAISE EXCEPTION 'coding workflow event tenant/run mismatch'; END IF;
  UPDATE workflow_delivery_control SET pending_count=pending_count+1 WHERE singleton AND pending_count<pending_limit;
  IF NOT FOUND THEN RAISE EXCEPTION 'workflow outbox global capacity exhausted'; END IF;
  INSERT INTO workflow_event_tenants(user_id) VALUES(NEW.user_id) ON CONFLICT DO NOTHING;
  UPDATE workflow_event_tenants SET pending_count=pending_count+1
    WHERE user_id=NEW.user_id AND pending_count<pending_limit RETURNING tenant_id INTO tenant;
  IF tenant IS NULL THEN RAISE EXCEPTION 'workflow outbox tenant capacity exhausted'; END IF;
  IF octet_length(NEW.phase)>96 THEN RAISE EXCEPTION 'workflow event phase exceeds bound'; END IF;
  body := convert_to(jsonb_build_object(
    'schema','gca.workflow.event.v2','workflow_kind','coding','event_id',identity,'tenant_id',tenant,
    'run_id',NEW.run_id,'sequence',seq,'source_event_id',NEW.id,'event_type',NEW.event_type,'phase',NEW.phase,
    'created_at',to_char(NEW.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))::text,'UTF8');
  INSERT INTO workflow_event_envelopes(event_id,source_event_id,tenant_id,run_id,sequence_no,
    event_type,wire_bytes,content_sha256,created_at,workflow_kind)
    VALUES(identity,NEW.id,tenant,NEW.run_id,seq,NEW.event_type,body,encode(sha256(body),'hex'),NEW.created_at,'coding');
  INSERT INTO workflow_event_outbox(event_id,tenant_id) VALUES(identity,tenant);
  RETURN NEW;
END $$;
CREATE TRIGGER workflow_coding_capture AFTER INSERT ON coding_run_events
  FOR EACH ROW EXECUTE FUNCTION capture_coding_workflow_event();

CREATE OR REPLACE FUNCTION workflow_tenant_privacy_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM workflow_event_tenants WHERE user_id=OLD.user_id
    AND NOT EXISTS(SELECT 1 FROM agent_runs WHERE user_id=OLD.user_id AND deleted_at IS NULL)
    AND NOT EXISTS(SELECT 1 FROM coding_runs WHERE user_id=OLD.user_id AND deleted_at IS NULL);
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION workflow_run_privacy_erased() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM workflow_event_envelopes WHERE run_id=NEW.id AND workflow_kind='workspace';
  RETURN NULL;
END $$;
CREATE FUNCTION workflow_coding_privacy_erased() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM workflow_event_envelopes WHERE run_id=NEW.id AND workflow_kind='coding';
  RETURN NULL;
END $$;
CREATE TRIGGER workflow_coding_soft_erased AFTER UPDATE OF deleted_at ON coding_runs
  FOR EACH ROW WHEN(OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)
  EXECUTE FUNCTION workflow_coding_privacy_erased();
CREATE CONSTRAINT TRIGGER workflow_coding_tenant_deleted AFTER DELETE ON coding_runs
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION workflow_tenant_privacy_cleanup();
CREATE CONSTRAINT TRIGGER workflow_coding_tenant_erased AFTER UPDATE ON coding_runs
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)
  EXECUTE FUNCTION workflow_tenant_privacy_cleanup();
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF (SELECT capture_coding_enabled FROM workflow_delivery_control WHERE singleton)
    OR EXISTS(SELECT 1 FROM workflow_event_envelopes e JOIN workflow_event_outbox d USING(event_id)
      WHERE e.workflow_kind='coding' AND d.state<>'published') THEN
    RAISE EXCEPTION 'disable and drain or reconcile coding delivery before downgrade';
  END IF;
END $$;
-- Only acknowledged, derived delivery metadata is removed; coding business facts survive.
DELETE FROM workflow_event_envelopes WHERE workflow_kind='coding';
DROP TRIGGER workflow_coding_capture ON coding_run_events;
DROP TRIGGER workflow_coding_soft_erased ON coding_runs;
DROP TRIGGER workflow_coding_tenant_deleted ON coding_runs;
DROP TRIGGER workflow_coding_tenant_erased ON coding_runs;
DROP FUNCTION capture_coding_workflow_event();
DROP FUNCTION workflow_coding_privacy_erased();
DROP TABLE workflow_coding_event_projection;
ALTER TABLE workflow_event_envelopes
  DROP CONSTRAINT workflow_workspace_parent,
  DROP CONSTRAINT workflow_coding_parent,
  DROP CONSTRAINT workflow_source_identity,
  DROP CONSTRAINT workflow_run_sequence,
  DROP COLUMN workspace_parent_id,
  DROP COLUMN coding_parent_id,
  DROP COLUMN workflow_kind,
  ADD CONSTRAINT workflow_event_envelopes_run_id_fkey FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
  ADD CONSTRAINT workflow_event_envelopes_source_event_id_key UNIQUE(source_event_id),
  ADD CONSTRAINT workflow_event_envelopes_run_id_sequence_no_key UNIQUE(run_id,sequence_no);
CREATE OR REPLACE FUNCTION workflow_tenant_privacy_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM workflow_event_tenants WHERE user_id=OLD.user_id
    AND NOT EXISTS(SELECT 1 FROM agent_runs WHERE user_id=OLD.user_id AND deleted_at IS NULL);
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION workflow_run_privacy_erased() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN DELETE FROM workflow_event_envelopes WHERE run_id=NEW.id; RETURN NULL; END $$;
ALTER TABLE coding_runs DROP COLUMN workflow_event_sequence;
ALTER TABLE workflow_delivery_control DROP COLUMN capture_coding_enabled;
ALTER TABLE workflow_delivery_audit DROP COLUMN workflow_kind;
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
