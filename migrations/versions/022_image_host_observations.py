"""Bind packaging recovery to the host journal without implying build success.

Revision ID: 022
Revises: 021
"""
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
ALTER TABLE coding_image_build_jobs
  ADD COLUMN host_job_id TEXT CHECK(host_job_id ~ '^[0-9a-f]{32}$'),
  ADD COLUMN host_observation_hash TEXT CHECK(host_observation_hash ~ '^[0-9a-f]{64}$'),
  ADD COLUMN host_observed_at TIMESTAMPTZ,
  ADD CONSTRAINT image_host_observation_pair
    CHECK((host_observation_hash IS NULL)=(host_observed_at IS NULL)),
  ADD CONSTRAINT image_host_observation_identity
    CHECK(host_observation_hash IS NULL OR (host_job_id IS NOT NULL AND dispatch_token IS NOT NULL)),
  ADD CONSTRAINT image_host_job_identity UNIQUE(pool_id,host_job_id);
"""


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM coding_image_build_jobs
        WHERE host_job_id IS NOT NULL AND storage_released_at IS NULL) THEN
        RAISE EXCEPTION 'reconcile image host obligations before downgrade';
      END IF;
    END $$;
    ALTER TABLE coding_image_build_jobs
      DROP CONSTRAINT image_host_job_identity,
      DROP CONSTRAINT image_host_observation_identity,
      DROP CONSTRAINT image_host_observation_pair,
      DROP COLUMN host_job_id,
      DROP COLUMN host_observation_hash,
      DROP COLUMN host_observed_at;
    """)
