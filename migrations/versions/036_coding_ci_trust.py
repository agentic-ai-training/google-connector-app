"""Human-reviewed immutable repository CI trust policies; no seeded trust.

Revision ID: 036
Revises: 035
"""
from alembic import op

revision = '036'
down_revision = '035'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
CREATE TABLE coding_ci_policies (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  repository TEXT NOT NULL CHECK(repository ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  workflow_id BIGINT NOT NULL CHECK(workflow_id>0),
  workflow_path TEXT NOT NULL CHECK(workflow_path ~ '^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$'),
  trusted_commit TEXT NOT NULL CHECK(trusted_commit ~ '^[0-9a-f]{40}$'),
  workflow_sha256 TEXT NOT NULL CHECK(workflow_sha256 ~ '^[0-9a-f]{64}$'),
  required_jobs JSONB NOT NULL CHECK(jsonb_typeof(required_jobs)='object' AND required_jobs<>'{}'::jsonb
    AND octet_length(required_jobs::text)<=32000),
  approved_by TEXT NOT NULL CHECK(length(approved_by) BETWEEN 1 AND 300),
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL,
  CHECK(expires_at>created_at AND expires_at<=created_at+interval '30 days')
);
CREATE INDEX coding_ci_latest ON coding_ci_policies(repository,created_at DESC,id DESC);
REVOKE ALL ON coding_ci_policies FROM PUBLIC;
CREATE FUNCTION serialize_coding_ci_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('coding-ci-policy:'||NEW.repository,0));
  RETURN NEW;
END $$;
CREATE TRIGGER coding_ci_serialize BEFORE INSERT ON coding_ci_policies
  FOR EACH ROW EXECUTE FUNCTION serialize_coding_ci_policy();
CREATE FUNCTION immutable_coding_ci_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'CI trust policy is immutable; publish a separately reviewed replacement'; END $$;
CREATE TRIGGER coding_ci_immutable BEFORE UPDATE OR DELETE ON coding_ci_policies
  FOR EACH ROW EXECUTE FUNCTION immutable_coding_ci_policy();
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute('''DO $$ BEGIN IF EXISTS(SELECT 1 FROM coding_ci_policies) THEN
      RAISE EXCEPTION 'retain reviewed CI trust policies until an explicit migration plan exists';
      END IF; END $$;
      DROP TABLE coding_ci_policies; DROP FUNCTION immutable_coding_ci_policy(); DROP FUNCTION serialize_coding_ci_policy();''')
