"""Single-use PostgreSQL verified-prefix continuation grants.

Revision ID: 033
Revises: 032
"""
from alembic import op

revision = '033'
down_revision = '032'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
CREATE TABLE tool_prefix_continuations (
  id UUID PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  step_id UUID NOT NULL,
  prior_attempt INTEGER NOT NULL CHECK(prior_attempt>0),
  next_attempt INTEGER NOT NULL CHECK(next_attempt=prior_attempt+1),
  source_hash TEXT NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
  receipt_digest TEXT NOT NULL CHECK(receipt_digest ~ '^[0-9a-f]{64}$'),
  prefix_length SMALLINT NOT NULL CHECK(prefix_length BETWEEN 1 AND 63),
  executor_version TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  authority_hash TEXT CHECK(authority_hash IS NULL OR authority_hash ~ '^[0-9a-f]{64}$'),
  FOREIGN KEY(run_id,step_id) REFERENCES agent_run_steps(run_id,id) ON DELETE CASCADE,
  UNIQUE(run_id,step_id,next_attempt),
  CHECK(expires_at>created_at AND expires_at<=created_at+interval '15 minutes'),
  CHECK((consumed_at IS NULL)=(authority_hash IS NULL))
);
REVOKE ALL ON tool_prefix_continuations FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') THEN
    REVOKE ALL ON tool_prefix_continuations FROM dbeaver_analyst;
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='grafana_reader') THEN
    REVOKE ALL ON tool_prefix_continuations FROM grafana_reader;
  END IF;
END $$;
CREATE FUNCTION protect_tool_prefix_continuation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.id,NEW.run_id,NEW.step_id,NEW.prior_attempt,NEW.next_attempt,NEW.source_hash,
    NEW.receipt_digest,NEW.prefix_length,NEW.executor_version,NEW.created_at,NEW.expires_at)
    IS DISTINCT FROM ROW(OLD.id,OLD.run_id,OLD.step_id,OLD.prior_attempt,OLD.next_attempt,OLD.source_hash,
    OLD.receipt_digest,OLD.prefix_length,OLD.executor_version,OLD.created_at,OLD.expires_at)
    OR OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL
    OR NEW.authority_hash IS NULL OR NEW.expires_at<=clock_timestamp() THEN
    RAISE EXCEPTION 'prefix continuation is immutable or expired';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER protect_tool_prefix_continuation BEFORE UPDATE ON tool_prefix_continuations
FOR EACH ROW EXECUTE FUNCTION protect_tool_prefix_continuation();
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM tool_prefix_continuations) THEN
    RAISE EXCEPTION 'prefix continuation evidence must be retained';
  END IF;
END $$;
DROP TABLE tool_prefix_continuations;
DROP FUNCTION protect_tool_prefix_continuation();
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
