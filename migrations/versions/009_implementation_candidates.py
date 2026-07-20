"""Require concrete, validated artifacts before improvement canaries.

Revision ID: 009
Revises: 008
"""

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
ALTER TABLE improvement_proposals
  ADD COLUMN candidate_kind TEXT NOT NULL DEFAULT 'diagnosis'
    CHECK(candidate_kind IN ('diagnosis','code','okf','config','prompt')),
  ADD COLUMN candidate_state TEXT NOT NULL DEFAULT 'diagnosis_only'
    CHECK(candidate_state IN
      ('diagnosis_only','implementation_draft','validated_implementation','deployed_canary')),
  ADD COLUMN candidate_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN validation_report JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN deployment_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE improvement_proposals
SET candidate_version=NULL,candidate_state='diagnosis_only',candidate_kind='diagnosis',
    candidate_manifest=jsonb_build_object(
      'migration_note','Legacy recommendation did not contain executable artifacts')
WHERE candidate_version LIKE 'candidate-%';

CREATE TABLE improvement_candidate_files (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_id UUID NOT NULL REFERENCES improvement_proposals(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('create','replace','delete')),
    content TEXT,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(proposal_id,path),
    CHECK(path !~ '(^/|(^|/)\.\.(/|$))'),
    CHECK((change_type='delete' AND content IS NULL) OR
          (change_type!='delete' AND content IS NOT NULL))
);

DROP VIEW reporting.improvement_queue;
CREATE VIEW reporting.improvement_queue AS
SELECT proposal_key,proposal_type,title,status,severity,risk_level,
       affected_sessions,root_cause_confidence,source_version,candidate_version,
       candidate_kind,candidate_state,validation_report,deployment_evidence,
       created_at,expires_at
FROM improvement_proposals;
''')


def downgrade():
    op.execute(r'''
DROP VIEW reporting.improvement_queue;
DROP TABLE IF EXISTS improvement_candidate_files;
CREATE VIEW reporting.improvement_queue AS
SELECT proposal_key, proposal_type, title, status, severity, risk_level,
       affected_sessions, root_cause_confidence, source_version,
       candidate_version, created_at, expires_at
FROM improvement_proposals;
ALTER TABLE improvement_proposals
  DROP COLUMN IF EXISTS deployment_evidence,
  DROP COLUMN IF EXISTS validation_report,
  DROP COLUMN IF EXISTS candidate_manifest,
  DROP COLUMN IF EXISTS candidate_state,
  DROP COLUMN IF EXISTS candidate_kind;
''')
