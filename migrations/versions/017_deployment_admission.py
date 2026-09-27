"""Immutable tenant targets and atomic budget-bound deployment admission.

Revision ID: 017
Revises: 016
"""
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
ALTER TABLE cloud_cost_reservations ADD CONSTRAINT cloud_cost_reservation_owner_key
    UNIQUE(id,project_id,user_id);
CREATE TABLE coding_deployment_targets (
    target_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES cloud_budget_accounts(project_id),
    config JSONB NOT NULL,
    config_hash TEXT NOT NULL CHECK(config_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
    revoked_at TIMESTAMPTZ,
    evidence_reference TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(target_id,user_id),
    UNIQUE(target_id,user_id,project_id)
);
CREATE TABLE coding_deployment_grants (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    manifest_hash TEXT NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
    authority_reference TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id,manifest_hash),
    UNIQUE(id,target_id,user_id),
    FOREIGN KEY(target_id,user_id) REFERENCES coding_deployment_targets(target_id,user_id)
);
CREATE TABLE coding_deployments (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES cloud_budget_accounts(project_id),
    grant_id UUID NOT NULL UNIQUE REFERENCES coding_deployment_grants(id),
    reservation_id UUID NOT NULL UNIQUE REFERENCES cloud_cost_reservations(id),
    manifest JSONB NOT NULL,
    manifest_hash TEXT NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
      CHECK(status IN ('reserved','dispatching','uncertain','submitted','verified','failed','cancelled','retired')),
    provider_resource_id TEXT,
    provider_evidence_hash TEXT CHECK(provider_evidence_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id,idempotency_key),
    FOREIGN KEY(target_id,user_id,project_id)
      REFERENCES coding_deployment_targets(target_id,user_id,project_id),
    FOREIGN KEY(grant_id,target_id,user_id)
      REFERENCES coding_deployment_grants(id,target_id,user_id),
    FOREIGN KEY(reservation_id,project_id,user_id)
      REFERENCES cloud_cost_reservations(id,project_id,user_id)
);
CREATE TABLE coding_deployment_events (
    id BIGSERIAL PRIMARY KEY,
    deployment_id UUID NOT NULL REFERENCES coding_deployments(id),
    event_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    op.execute("DROP TABLE coding_deployment_events; DROP TABLE coding_deployments; "
               "DROP TABLE coding_deployment_grants; DROP TABLE coding_deployment_targets; "
               "ALTER TABLE cloud_cost_reservations DROP CONSTRAINT cloud_cost_reservation_owner_key;")
