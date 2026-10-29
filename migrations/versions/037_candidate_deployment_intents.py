"""Write-ahead core-candidate deployment authority and transition history.

Revision ID: 037
Revises: 036
"""
from alembic import op

revision = '037'
down_revision = '036'
branch_labels = None
depends_on = None

SCHEMA_SQL = r'''
CREATE TABLE candidate_deployment_intents (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  proposal_id UUID REFERENCES improvement_proposals(id) ON DELETE SET NULL,
  approval_id UUID REFERENCES improvement_approvals(id) ON DELETE SET NULL,
  frozen JSONB NOT NULL CHECK(jsonb_typeof(frozen)='object' AND octet_length(frozen::text)<=16000),
  state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN
    ('prepared','dispatch_unknown','dispatched','claimed','attested','cancelled')),
  workflow_run_id BIGINT CHECK(workflow_run_id>0),
  workflow_attempt INTEGER CHECK(workflow_attempt=1),
  nonce_hash TEXT CHECK(nonce_hash ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL,
  claimed_at TIMESTAMPTZ,
  claim_expires_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  diagnostic TEXT CHECK(diagnostic IN ('dispatch_response_unknown','dispatch_acknowledged','human_cancelled')),
  last_observation JSONB NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(last_observation)='object'
    AND octet_length(last_observation::text)<=4000),
  UNIQUE(approval_id),
  CHECK(expires_at>created_at AND expires_at<=created_at+interval '1 hour'),
  CHECK((state IN ('claimed','attested')) = (nonce_hash IS NOT NULL)),
  CHECK(nonce_hash IS NULL OR (workflow_run_id IS NOT NULL AND workflow_attempt=1
    AND claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL))
);
CREATE TABLE candidate_deployment_intent_events (
  sequence BIGSERIAL PRIMARY KEY,
  intent_id UUID NOT NULL REFERENCES candidate_deployment_intents(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  workflow_run_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX candidate_deployment_intent_proposal ON candidate_deployment_intents(proposal_id,created_at DESC);
REVOKE ALL ON candidate_deployment_intents,candidate_deployment_intent_events FROM PUBLIC;
CREATE FUNCTION fence_candidate_deployment_intent() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='UPDATE' THEN
    IF NEW.frozen IS DISTINCT FROM OLD.frozen OR NEW.created_at<>OLD.created_at
      OR NEW.expires_at<>OLD.expires_at OR NEW.id<>OLD.id
      OR (NEW.proposal_id IS DISTINCT FROM OLD.proposal_id AND NEW.proposal_id IS NOT NULL)
      OR (NEW.approval_id IS DISTINCT FROM OLD.approval_id AND NEW.approval_id IS NOT NULL) THEN
      RAISE EXCEPTION 'deployment authority is immutable';
    END IF;
    IF NEW.state<>OLD.state AND NOT (
      (OLD.state='prepared' AND NEW.state IN ('dispatch_unknown','cancelled')) OR
      (OLD.state='dispatch_unknown' AND NEW.state IN ('dispatched','claimed','cancelled')) OR
      (OLD.state='dispatched' AND NEW.state IN ('claimed','cancelled')) OR
      (OLD.state='claimed' AND NEW.state='attested')) THEN
      RAISE EXCEPTION 'deployment transition requires reconciliation, not replay';
    END IF;
    IF OLD.workflow_run_id IS NOT NULL AND
      (NEW.workflow_run_id IS DISTINCT FROM OLD.workflow_run_id OR NEW.workflow_attempt IS DISTINCT FROM OLD.workflow_attempt) THEN
      RAISE EXCEPTION 'deployment workflow identity is immutable';
    END IF;
    IF OLD.nonce_hash IS NOT NULL AND (NEW.nonce_hash IS DISTINCT FROM OLD.nonce_hash
      OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at OR NEW.claim_expires_at IS DISTINCT FROM OLD.claim_expires_at) THEN
      RAISE EXCEPTION 'deployment claim cannot be renewed';
    END IF;
  END IF;
  NEW.updated_at=clock_timestamp();
  RETURN NEW;
END $$;
CREATE TRIGGER candidate_deployment_intent_fence BEFORE UPDATE ON candidate_deployment_intents
  FOR EACH ROW EXECUTE FUNCTION fence_candidate_deployment_intent();
CREATE FUNCTION record_candidate_deployment_intent() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='INSERT' OR NEW.state IS DISTINCT FROM OLD.state OR NEW.workflow_run_id IS DISTINCT FROM OLD.workflow_run_id THEN
    INSERT INTO candidate_deployment_intent_events(intent_id,state,workflow_run_id)
      VALUES(NEW.id,NEW.state,NEW.workflow_run_id);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER candidate_deployment_intent_history AFTER INSERT OR UPDATE ON candidate_deployment_intents
  FOR EACH ROW EXECUTE FUNCTION record_candidate_deployment_intent();
'''


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute('''DO $$ BEGIN IF EXISTS(SELECT 1 FROM candidate_deployment_intents) THEN
      RAISE EXCEPTION 'retain deployment authority and reconcile external effects before downgrade';
      END IF; END $$;
      DROP TABLE candidate_deployment_intent_events; DROP TABLE candidate_deployment_intents;
      DROP FUNCTION record_candidate_deployment_intent(); DROP FUNCTION fence_candidate_deployment_intent();''')
