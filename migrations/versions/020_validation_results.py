"""Encrypted, expiring validation results separate from resource cleanup.

Revision ID: 020
Revises: 019
"""
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
ALTER TABLE coding_validation_claims
    ADD COLUMN validation_outcome TEXT NOT NULL DEFAULT 'pending'
      CHECK(validation_outcome IN ('pending','unknown','passed','failed')),
    ADD COLUMN result_hash TEXT CHECK(result_hash ~ '^[0-9a-f]{64}$'),
    ADD COLUMN encrypted_result TEXT,
    ADD COLUMN result_recorded_at TIMESTAMPTZ,
    ADD COLUMN result_expires_at TIMESTAMPTZ,
    ADD CONSTRAINT validation_result_consistent CHECK (
      (validation_outcome IN ('passed','failed'))=(result_hash IS NOT NULL)
      AND (result_hash IS NOT NULL)=(result_recorded_at IS NOT NULL)
      AND (result_hash IS NOT NULL)=(result_expires_at IS NOT NULL)
      AND (encrypted_result IS NULL OR result_hash IS NOT NULL)
      AND (result_expires_at IS NULL OR result_expires_at>result_recorded_at));
UPDATE coding_validation_claims SET validation_outcome='unknown'
    WHERE status IN ('dispatched','uncertain','released');
CREATE INDEX coding_validation_result_expiry ON coding_validation_claims(result_expires_at)
    WHERE encrypted_result IS NOT NULL;
ALTER TABLE coding_validation_claims
    DROP CONSTRAINT coding_validation_claims_run_id_user_id_fkey,
    ALTER COLUMN run_id DROP NOT NULL,
    ADD CONSTRAINT coding_validation_claims_run_id_user_id_fkey
      FOREIGN KEY(run_id,user_id) REFERENCES coding_runs(id,user_id)
      ON DELETE SET NULL (run_id);
CREATE FUNCTION scrub_orphan_validation_result() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.run_id IS NULL THEN NEW.encrypted_result := NULL; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER validation_result_run_deleted BEFORE INSERT OR UPDATE
  ON coding_validation_claims FOR EACH ROW EXECUTE FUNCTION scrub_orphan_validation_result();
"""


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM coding_validation_claims WHERE encrypted_result IS NOT NULL) THEN
        RAISE EXCEPTION 'preserve or expire private validation results before downgrade';
      END IF;
      IF EXISTS(SELECT 1 FROM coding_validation_claims WHERE run_id IS NULL) THEN
        RAISE EXCEPTION 'preserve orphan cleanup obligations before downgrade';
      END IF;
    END $$;
    DROP TRIGGER validation_result_run_deleted ON coding_validation_claims;
    DROP FUNCTION scrub_orphan_validation_result();
    ALTER TABLE coding_validation_claims
      DROP CONSTRAINT coding_validation_claims_run_id_user_id_fkey,
      ALTER COLUMN run_id SET NOT NULL,
      ADD CONSTRAINT coding_validation_claims_run_id_user_id_fkey
        FOREIGN KEY(run_id,user_id) REFERENCES coding_runs(id,user_id);
    DROP INDEX coding_validation_result_expiry;
    ALTER TABLE coding_validation_claims DROP CONSTRAINT validation_result_consistent,
      DROP COLUMN result_expires_at, DROP COLUMN result_recorded_at,
      DROP COLUMN encrypted_result, DROP COLUMN result_hash, DROP COLUMN validation_outcome;
    """)
