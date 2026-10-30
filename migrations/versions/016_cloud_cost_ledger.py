"""Atomic project-wide cloud cost reservations and reconciliation.

Revision ID: 016
Revises: 015
"""
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
CREATE TABLE cloud_budget_accounts (
    project_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR' CHECK (currency='INR'),
    opening_credit_paise BIGINT NOT NULL CHECK (opening_credit_paise>=0),
    observed_credit_paise BIGINT NOT NULL CHECK (observed_credit_paise>=0),
    observed_at TIMESTAMPTZ NOT NULL,
    credit_expires_at TIMESTAMPTZ NOT NULL,
    hosting_ends_at TIMESTAMPTZ NOT NULL,
    evidence_reference TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (hosting_ends_at<=credit_expires_at)
);
CREATE TABLE cloud_cost_reservations (
    id UUID PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES cloud_budget_accounts(project_id),
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    category TEXT NOT NULL CHECK (category IN ('hosting','vertex')),
    estimated_paise BIGINT NOT NULL CHECK (estimated_paise>0),
    planned_end_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
      CHECK (status IN ('reserved','dispatched','uncertain','settled','released')),
    actual_paise BIGINT CHECK (actual_paise>=0),
    accrued_paise BIGINT NOT NULL DEFAULT 0 CHECK (accrued_paise>=0),
    accrual_observed_at TIMESTAMPTZ,
    accrual_evidence_reference TEXT,
    evidence_reference TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,user_id,idempotency_key),
    CHECK ((status='settled')=(actual_paise IS NOT NULL)),
    CHECK (actual_paise IS NULL OR actual_paise>=accrued_paise),
    CHECK (status NOT IN ('reserved','released') OR accrued_paise=0),
    CHECK (status NOT IN ('settled','released') OR evidence_reference IS NOT NULL)
);
CREATE INDEX cloud_cost_reservations_account_idx
    ON cloud_cost_reservations(project_id,category,status);
CREATE TABLE cloud_cost_events (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES cloud_budget_accounts(project_id),
    reservation_id UUID REFERENCES cloud_cost_reservations(id),
    event_type TEXT NOT NULL,
    evidence_reference TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    # Financial evidence must be exported before a deliberately authorized downgrade.
    op.execute("DROP TABLE cloud_cost_events; DROP TABLE cloud_cost_reservations; "
               "DROP TABLE cloud_budget_accounts;")
