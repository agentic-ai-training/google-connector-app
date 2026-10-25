"""Private per-invocation write-ahead evidence, independent of step completion.

Revision ID: 031
Revises: 030
"""
from alembic import op

revision = '031'
down_revision = '030'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
ALTER TABLE agent_run_steps ADD CONSTRAINT agent_step_run_identity UNIQUE(run_id,id);
CREATE TABLE agent_tool_receipts (
  id UUID PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  step_id UUID NOT NULL,
  step_attempt INTEGER NOT NULL CHECK(step_attempt>0),
  sequence_no SMALLINT NOT NULL CHECK(sequence_no BETWEEN 1 AND 64),
  tool_name TEXT NOT NULL CHECK(tool_name ~ '^[A-Za-z0-9_]{1,100}$'),
  call_hash TEXT NOT NULL CHECK(call_hash ~ '^[0-9a-f]{64}$'),
  authority_hash TEXT NOT NULL CHECK(authority_hash ~ '^[0-9a-f]{64}$'),
  read_only BOOLEAN NOT NULL,
  state TEXT NOT NULL DEFAULT 'dispatched' CHECK(state IN ('dispatched','returned','unknown')),
  encrypted_payload TEXT CHECK(encrypted_payload IS NULL OR octet_length(encrypted_payload) BETWEEN 1 AND 2700000),
  payload_hash TEXT NOT NULL CHECK(payload_hash ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  completed_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY(run_id,step_id) REFERENCES agent_run_steps(run_id,id) ON DELETE CASCADE,
  UNIQUE(run_id,step_id,sequence_no),
  UNIQUE(run_id,step_id,step_attempt,call_hash),
  CHECK(expires_at>created_at AND expires_at<=created_at+interval '24 hours'),
  CHECK((state='dispatched' AND completed_at IS NULL) OR (state<>'dispatched' AND completed_at IS NOT NULL))
);
CREATE INDEX tool_receipt_expiry ON agent_tool_receipts(expires_at) WHERE encrypted_payload IS NOT NULL;
REVOKE ALL ON agent_tool_receipts FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') THEN
    REVOKE ALL ON agent_tool_receipts FROM dbeaver_analyst;
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='grafana_reader') THEN
    REVOKE ALL ON agent_tool_receipts FROM grafana_reader;
  END IF;
END $$;
CREATE FUNCTION protect_tool_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.id,NEW.run_id,NEW.step_id,NEW.step_attempt,NEW.sequence_no,NEW.tool_name,NEW.call_hash,
    NEW.authority_hash,NEW.read_only,NEW.created_at,NEW.expires_at)
    IS DISTINCT FROM ROW(OLD.id,OLD.run_id,OLD.step_id,OLD.step_attempt,OLD.sequence_no,OLD.tool_name,OLD.call_hash,
    OLD.authority_hash,OLD.read_only,OLD.created_at,OLD.expires_at) THEN
    RAISE EXCEPTION 'tool receipt binding is immutable';
  END IF;
  IF OLD.state<>'dispatched' OR NEW.state='dispatched' THEN
    IF ROW(NEW.state,NEW.completed_at,NEW.payload_hash) IS DISTINCT FROM ROW(OLD.state,OLD.completed_at,OLD.payload_hash)
       OR (NEW.encrypted_payload IS DISTINCT FROM OLD.encrypted_payload AND
         NOT (NEW.encrypted_payload IS NULL AND (OLD.expires_at<=clock_timestamp() OR EXISTS(
           SELECT 1 FROM agent_runs WHERE id=OLD.run_id AND deleted_at IS NOT NULL)))) THEN
      RAISE EXCEPTION 'tool receipt outcome is immutable except private erasure';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER tool_receipt_binding BEFORE UPDATE ON agent_tool_receipts
  FOR EACH ROW EXECUTE FUNCTION protect_tool_receipt();
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM agent_tool_receipts) THEN
    RAISE EXCEPTION 'reconcile retained tool invocation evidence before downgrade';
  END IF;
END $$;
DROP TABLE agent_tool_receipts;
DROP FUNCTION protect_tool_receipt();
ALTER TABLE agent_run_steps DROP CONSTRAINT agent_step_run_identity;
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
