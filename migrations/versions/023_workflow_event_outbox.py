"""Opt-in tenant-scoped immutable event envelopes and transactional delivery.

Revision ID: 023
Revises: 022
"""
from alembic import op

revision = '023'
down_revision = '022'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
ALTER TABLE agent_runs ADD COLUMN workflow_event_sequence BIGINT NOT NULL DEFAULT 0
  CHECK(workflow_event_sequence>=0);

CREATE TABLE workflow_delivery_control (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
  capture_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  pending_count BIGINT NOT NULL DEFAULT 0 CHECK(pending_count>=0),
  pending_limit INTEGER NOT NULL DEFAULT 50000 CHECK(pending_limit BETWEEN 1 AND 50000)
);
INSERT INTO workflow_delivery_control(singleton) VALUES(TRUE);
CREATE TABLE workflow_event_tenants (
  user_id TEXT PRIMARY KEY,
  tenant_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  pending_count BIGINT NOT NULL DEFAULT 0 CHECK(pending_count>=0),
  pending_limit INTEGER NOT NULL DEFAULT 2000 CHECK(pending_limit BETWEEN 1 AND 2000)
);
CREATE TABLE workflow_event_envelopes (
  event_id UUID PRIMARY KEY,
  source_event_id BIGINT NOT NULL UNIQUE,
  tenant_id UUID NOT NULL REFERENCES workflow_event_tenants(tenant_id) ON DELETE CASCADE,
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  sequence_no BIGINT NOT NULL CHECK(sequence_no>0),
  event_type TEXT NOT NULL CHECK(event_type ~ '^[a-zA-Z][a-zA-Z0-9_.-]{0,95}$'),
  wire_bytes BYTEA NOT NULL CHECK(octet_length(wire_bytes) BETWEEN 1 AND 16384),
  content_sha256 TEXT NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(run_id,sequence_no), UNIQUE(event_id,tenant_id)
);
CREATE INDEX workflow_envelopes_tenant ON workflow_event_envelopes(tenant_id,created_at);
CREATE TABLE workflow_event_outbox (
  event_id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  FOREIGN KEY(event_id,tenant_id) REFERENCES workflow_event_envelopes(event_id,tenant_id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','published','dead_letter')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 12),
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_owner TEXT,
  lease_token UUID,
  lease_expires_at TIMESTAMPTZ,
  published_at TIMESTAMPTZ,
  broker_partition INTEGER CHECK(broker_partition BETWEEN 0 AND 2),
  broker_offset BIGINT CHECK(broker_offset>=-1),
  failure_category TEXT CHECK(failure_category IN ('network','broker_unavailable','broker_rejected',
      'serialization','lease_expired','delivery_unconfirmed')),
  CHECK((state='leased')=(lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
  CHECK((lease_owner IS NULL)=(lease_token IS NULL) AND (lease_owner IS NULL)=(lease_expires_at IS NULL)),
  CHECK((state='published')=(published_at IS NOT NULL)),
  CHECK((state='published')=(broker_partition IS NOT NULL AND broker_offset IS NOT NULL)),
  CHECK((broker_partition IS NULL)=(broker_offset IS NULL))
);
CREATE INDEX workflow_outbox_ready ON workflow_event_outbox(available_at,event_id)
  WHERE state IN ('pending','leased');
CREATE TABLE workflow_event_inbox (
  consumer_name TEXT NOT NULL CHECK(consumer_name ~ '^[a-z][a-z0-9_.-]{0,63}$'),
  event_id UUID NOT NULL,
  tenant_id UUID NOT NULL,
  FOREIGN KEY(event_id,tenant_id) REFERENCES workflow_event_envelopes(event_id,tenant_id) ON DELETE CASCADE,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(consumer_name,event_id)
);
CREATE TABLE workflow_delivery_audit (
  id BIGSERIAL PRIMARY KEY,
  enabled BOOLEAN NOT NULL,
  evidence_reference TEXT NOT NULL CHECK(octet_length(evidence_reference) BETWEEN 1 AND 512),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION workflow_envelope_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'workflow event envelope is immutable'; END $$;
CREATE TRIGGER workflow_envelope_no_update BEFORE UPDATE ON workflow_event_envelopes
  FOR EACH ROW EXECUTE FUNCTION workflow_envelope_immutable();

CREATE FUNCTION capture_workflow_run_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  seq BIGINT;
  tenant UUID;
  identity UUID := gen_random_uuid();
  body BYTEA;
BEGIN
  IF NOT (SELECT capture_enabled FROM workflow_delivery_control WHERE singleton) THEN RETURN NEW; END IF;
  UPDATE agent_runs SET workflow_event_sequence=workflow_event_sequence+1
    WHERE id=NEW.run_id AND user_id=NEW.user_id AND deleted_at IS NULL
    RETURNING workflow_event_sequence INTO seq;
  IF seq IS NULL THEN RAISE EXCEPTION 'workflow event tenant/run mismatch'; END IF;
  UPDATE workflow_delivery_control SET pending_count=pending_count+1
    WHERE singleton AND pending_count<pending_limit;
  IF NOT FOUND THEN RAISE EXCEPTION 'workflow outbox global capacity exhausted'; END IF;
  INSERT INTO workflow_event_tenants(user_id) VALUES(NEW.user_id) ON CONFLICT DO NOTHING;
  UPDATE workflow_event_tenants SET pending_count=pending_count+1
    WHERE user_id=NEW.user_id AND pending_count<pending_limit RETURNING tenant_id INTO tenant;
  IF tenant IS NULL THEN RAISE EXCEPTION 'workflow outbox tenant capacity exhausted'; END IF;
  IF NEW.phase IS NOT NULL AND octet_length(NEW.phase)>96 THEN RAISE EXCEPTION 'workflow event phase exceeds bound'; END IF;
  body := convert_to(jsonb_build_object(
      'schema','gca.workflow.event.v1','event_id',identity,'tenant_id',tenant,
      'run_id',NEW.run_id,'sequence',seq,'source_event_id',NEW.id,
      'event_type',NEW.event_type,'phase',NEW.phase,
      'created_at',to_char(NEW.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))::text,'UTF8');
  INSERT INTO workflow_event_envelopes(event_id,source_event_id,tenant_id,run_id,sequence_no,
      event_type,wire_bytes,content_sha256,created_at)
    VALUES(identity,NEW.id,tenant,NEW.run_id,seq,NEW.event_type,body,encode(sha256(body),'hex'),NEW.created_at);
  INSERT INTO workflow_event_outbox(event_id,tenant_id) VALUES(identity,tenant);
  RETURN NEW;
END $$;
CREATE TRIGGER workflow_capture AFTER INSERT ON agent_run_events
  FOR EACH ROW EXECUTE FUNCTION capture_workflow_run_event();

CREATE FUNCTION workflow_pending_released() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state<>'published' THEN
    UPDATE workflow_delivery_control SET pending_count=pending_count-1 WHERE singleton;
    UPDATE workflow_event_tenants SET pending_count=pending_count-1 WHERE tenant_id=OLD.tenant_id;
  END IF;
  RETURN OLD;
END $$;
CREATE TRIGGER workflow_pending_deleted BEFORE DELETE ON workflow_event_outbox
  FOR EACH ROW EXECUTE FUNCTION workflow_pending_released();

CREATE FUNCTION workflow_tenant_privacy_cleanup() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM workflow_event_tenants WHERE user_id=OLD.user_id
    AND NOT EXISTS(SELECT 1 FROM agent_runs WHERE user_id=OLD.user_id AND deleted_at IS NULL);
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER workflow_tenant_deleted AFTER DELETE ON agent_runs
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION workflow_tenant_privacy_cleanup();
CREATE FUNCTION workflow_run_privacy_erased() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM workflow_event_envelopes WHERE run_id=NEW.id;
  RETURN NULL;
END $$;
CREATE TRIGGER workflow_run_soft_erased AFTER UPDATE OF deleted_at ON agent_runs
  FOR EACH ROW WHEN(OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)
  EXECUTE FUNCTION workflow_run_privacy_erased();
CREATE CONSTRAINT TRIGGER workflow_tenant_soft_erased AFTER UPDATE ON agent_runs
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
  WHEN(OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)
  EXECUTE FUNCTION workflow_tenant_privacy_cleanup();
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM workflow_event_outbox WHERE state<>'published') THEN
   RAISE EXCEPTION 'drain or reconcile workflow outbox before downgrade';
 END IF;
END $$;
DROP TRIGGER workflow_capture ON agent_run_events;
DROP TRIGGER workflow_tenant_deleted ON agent_runs;
DROP TRIGGER workflow_tenant_soft_erased ON agent_runs;
DROP TRIGGER workflow_run_soft_erased ON agent_runs;
DROP TABLE workflow_event_inbox;
DROP TABLE workflow_event_outbox;
DROP TABLE workflow_event_envelopes;
DROP TABLE workflow_event_tenants;
DROP TABLE workflow_delivery_control;
DROP TABLE workflow_delivery_audit;
DROP FUNCTION workflow_tenant_privacy_cleanup();
DROP FUNCTION workflow_run_privacy_erased();
DROP FUNCTION workflow_pending_released();
DROP FUNCTION capture_workflow_run_event();
DROP FUNCTION workflow_envelope_immutable();
ALTER TABLE agent_runs DROP COLUMN workflow_event_sequence;
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
