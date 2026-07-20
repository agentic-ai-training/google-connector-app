"""Durable hierarchical RAG parent sections.

Revision ID: 011
Revises: 010
"""

from alembic import op


revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
CREATE TABLE rag_parent_sections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    acl JSONB NOT NULL DEFAULT '{}'::jsonb,
    chunker_version TEXT NOT NULL,
    source_modified_at TIMESTAMPTZ,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE(user_id,source_type,source_id,parent_id,chunker_version)
);
CREATE INDEX rag_parent_sections_lookup_idx
  ON rag_parent_sections(user_id,source_type,source_id,parent_id)
  WHERE deleted_at IS NULL;

CREATE OR REPLACE VIEW reporting.rag_parent_lineage AS
SELECT p.user_id,p.source_type,p.source_id,p.parent_id,p.heading,p.content_hash,
       p.chunker_version,p.source_modified_at,p.indexed_at,p.deleted_at,
       count(c.id) FILTER(WHERE c.deleted_at IS NULL) AS active_child_chunks
FROM rag_parent_sections p
LEFT JOIN rag_chunks c
  ON c.user_id=p.user_id AND c.source_type=p.source_type
 AND c.source_id=p.source_id AND c.parent_id=p.parent_id
 AND c.chunker_version=p.chunker_version
GROUP BY p.user_id,p.source_type,p.source_id,p.parent_id,p.heading,p.content_hash,
         p.chunker_version,p.source_modified_at,p.indexed_at,p.deleted_at;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dbeaver_analyst') THEN
    GRANT SELECT ON reporting.rag_parent_lineage TO dbeaver_analyst;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='grafana_reader') THEN
    GRANT SELECT ON reporting.rag_parent_lineage TO grafana_reader;
  END IF;
END $$;
''')


def downgrade():
    op.execute(r'''
DROP VIEW IF EXISTS reporting.rag_parent_lineage;
DROP TABLE IF EXISTS rag_parent_sections;
''')
