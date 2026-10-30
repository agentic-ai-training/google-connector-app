"""Private write-ahead coding publication evidence, not business completion.

Revision ID: 035
Revises: 034
"""
from alembic import op

revision = '035'
down_revision = '034'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
CREATE TABLE coding_publication_receipts (
  run_id UUID PRIMARY KEY REFERENCES coding_runs(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  source_hash TEXT NOT NULL CHECK(source_hash ~ '^[0-9a-f]{64}$'),
  manifest_hash TEXT NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
  encrypted_manifest TEXT CHECK(encrypted_manifest IS NULL OR octet_length(encrypted_manifest) BETWEEN 1 AND 12000000),
  authority_hash TEXT NOT NULL CHECK(authority_hash ~ '^[0-9a-f]{64}$'),
  state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN ('prepared','dispatched','returned')),
  encrypted_result TEXT CHECK(encrypted_result IS NULL OR octet_length(encrypted_result) BETWEEN 1 AND 16000),
  result_hash TEXT CHECK(result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  dispatched_at TIMESTAMPTZ,
  returned_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()+interval '24 hours'-interval '1 second',
  CHECK(expires_at>created_at AND expires_at<=created_at+interval '24 hours'),
  CHECK((state='prepared' AND dispatched_at IS NULL AND returned_at IS NULL AND result_hash IS NULL)
     OR (state='dispatched' AND dispatched_at IS NOT NULL AND returned_at IS NULL AND result_hash IS NULL)
     OR (state='returned' AND dispatched_at IS NOT NULL AND returned_at IS NOT NULL AND result_hash IS NOT NULL))
);
CREATE INDEX coding_publication_expiry ON coding_publication_receipts(expires_at)
  WHERE encrypted_manifest IS NOT NULL OR encrypted_result IS NOT NULL;
REVOKE ALL ON coding_publication_receipts FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') THEN
    REVOKE ALL ON coding_publication_receipts FROM dbeaver_analyst;
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='grafana_reader') THEN
    REVOKE ALL ON coding_publication_receipts FROM grafana_reader;
  END IF;
END $$;
CREATE FUNCTION protect_coding_publication_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.run_id,NEW.user_id,NEW.source_hash,NEW.manifest_hash,NEW.authority_hash,NEW.created_at,NEW.expires_at)
    IS DISTINCT FROM ROW(OLD.run_id,OLD.user_id,OLD.source_hash,OLD.manifest_hash,OLD.authority_hash,OLD.created_at,OLD.expires_at)
    OR (NEW.encrypted_manifest IS DISTINCT FROM OLD.encrypted_manifest AND NEW.encrypted_manifest IS NOT NULL)
    OR NOT (NEW.state=OLD.state OR (OLD.state='prepared' AND NEW.state='dispatched')
         OR (OLD.state='dispatched' AND NEW.state='returned')) THEN
    RAISE EXCEPTION 'coding publication binding is immutable';
  END IF;
  IF (NEW.encrypted_manifest IS NULL AND OLD.encrypted_manifest IS NOT NULL)
      OR (NEW.encrypted_result IS NULL AND OLD.encrypted_result IS NOT NULL) THEN
    IF OLD.expires_at>clock_timestamp() AND NOT EXISTS(
      SELECT 1 FROM coding_runs WHERE id=OLD.run_id AND deleted_at IS NOT NULL) THEN
      RAISE EXCEPTION 'coding publication erasure requires expiry or deleted run';
    END IF;
  END IF;
  IF OLD.state='returned' AND (ROW(NEW.result_hash,NEW.returned_at) IS DISTINCT FROM ROW(OLD.result_hash,OLD.returned_at)
    OR (NEW.encrypted_result IS DISTINCT FROM OLD.encrypted_result AND NEW.encrypted_result IS NOT NULL)) THEN
    RAISE EXCEPTION 'coding publication result is immutable';
  END IF;
  IF OLD.dispatched_at IS NOT NULL AND NEW.dispatched_at IS DISTINCT FROM OLD.dispatched_at THEN
    RAISE EXCEPTION 'coding publication dispatch is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER coding_publication_binding BEFORE UPDATE ON coding_publication_receipts
  FOR EACH ROW EXECUTE FUNCTION protect_coding_publication_receipt();
'''

DOWNGRADE_SQL = r'''
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM coding_publication_receipts) THEN
    RAISE EXCEPTION 'reconcile retained coding publication evidence before downgrade';
  END IF;
END $$;
DROP TABLE coding_publication_receipts;
DROP FUNCTION protect_coding_publication_receipt();
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute(DOWNGRADE_SQL)
