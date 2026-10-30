"""Derived workflow projection, bounded failure ledger and privacy tombstones.

Revision ID: 024
Revises: 023
"""
from alembic import op

revision = '024'
down_revision = '023'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
CREATE TABLE workflow_run_event_projection (
  run_id UUID PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL,
  last_event_id UUID NOT NULL,
  last_sequence BIGINT NOT NULL CHECK(last_sequence>0),
  observed_event_count BIGINT NOT NULL DEFAULT 1 CHECK(observed_event_count>0),
  last_event_type TEXT NOT NULL,
  last_phase TEXT,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY(last_event_id,tenant_id) REFERENCES workflow_event_envelopes(event_id,tenant_id) ON DELETE CASCADE
);
CREATE INDEX workflow_projection_tenant ON workflow_run_event_projection(tenant_id,observed_at);

-- No event/run/user IDs or raw bytes: only proof that these exact bytes were erased.
CREATE TABLE workflow_event_revocations (
  content_sha256 TEXT PRIMARY KEY CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT now()+interval '48 hours'
);
CREATE INDEX workflow_revocations_expiry ON workflow_event_revocations(expires_at);
CREATE FUNCTION workflow_envelope_revoked() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO workflow_event_revocations(content_sha256) VALUES(OLD.content_sha256)
    ON CONFLICT(content_sha256) DO NOTHING;
  RETURN OLD;
END $$;
CREATE TRIGGER workflow_envelope_before_delete BEFORE DELETE ON workflow_event_envelopes
  FOR EACH ROW EXECUTE FUNCTION workflow_envelope_revoked();

CREATE TABLE workflow_consumer_control (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton)
);
INSERT INTO workflow_consumer_control(singleton) VALUES(TRUE);
CREATE TABLE workflow_consumer_failures (
  topic TEXT NOT NULL CHECK(topic='gca.workflow.events.v1'),
  partition_id INTEGER NOT NULL CHECK(partition_id BETWEEN 0 AND 2),
  broker_offset BIGINT NOT NULL CHECK(broker_offset>=0),
  content_sha256 TEXT CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
  category TEXT NOT NULL CHECK(category IN ('record_invalid','key_mismatch','source_unavailable',
    'projection_failed','commit_unconfirmed','assignment_lost')),
  attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts BETWEEN 1 AND 3),
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(topic,partition_id,broker_offset)
);
CREATE INDEX workflow_consumer_failures_retention ON workflow_consumer_failures(last_seen_at);
'''

DOWNGRADE_SQL = '''
DROP TRIGGER workflow_envelope_before_delete ON workflow_event_envelopes;
DROP FUNCTION workflow_envelope_revoked();
DROP TABLE workflow_consumer_failures;
DROP TABLE workflow_consumer_control;
DROP TABLE workflow_event_revocations;
DROP TABLE workflow_run_event_projection;
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    # Operator must stop the consumer and export/reconcile blocked records first.
    # These are derived SQL views/diagnostics, never business execution authority.
    op.execute(DOWNGRADE_SQL)
