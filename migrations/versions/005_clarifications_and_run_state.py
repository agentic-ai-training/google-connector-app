"""Durable clarification state.

Revision ID: 005
Revises: 004
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r'''
ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
  CHECK (status IN ('queued','awaiting_clarification','awaiting_approval','running',
                    'completed','partial','failed','cancelled'));
ALTER TABLE agent_runs ADD COLUMN clarification_questions JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE agent_runs ADD COLUMN clarification_answers JSONB NOT NULL DEFAULT '{}'::jsonb;
''')


def downgrade():
    op.execute(r'''
UPDATE agent_runs SET status='failed',error_category='user_input'
 WHERE status='awaiting_clarification';
ALTER TABLE agent_runs DROP COLUMN clarification_answers;
ALTER TABLE agent_runs DROP COLUMN clarification_questions;
ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
  CHECK (status IN ('queued','awaiting_approval','running','completed','partial','failed','cancelled'));
''')
