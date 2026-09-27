"""Capacity allocations on an already-funded host, without double-reserving its bill.

Revision ID: 018
Revises: 017
"""
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
CREATE TABLE coding_host_capacity (
    pool_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES cloud_budget_accounts(project_id),
    server_uuid TEXT NOT NULL,
    funding_reservation_id UUID NOT NULL UNIQUE REFERENCES cloud_cost_reservations(id),
    cpu_millis INTEGER NOT NULL CHECK(cpu_millis>0),
    memory_mib BIGINT NOT NULL CHECK(memory_mib>0),
    storage_mib BIGINT NOT NULL CHECK(storage_mib>0),
    enforcement_hash TEXT NOT NULL CHECK(enforcement_hash ~ '^[0-9a-f]{64}$'),
    evidence_reference TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(project_id,server_uuid),
    UNIQUE(pool_id,project_id)
);
ALTER TABLE coding_deployments ALTER COLUMN reservation_id DROP NOT NULL;
ALTER TABLE coding_deployments ADD COLUMN capacity_pool_id TEXT;
ALTER TABLE coding_deployments ADD COLUMN cpu_millis INTEGER NOT NULL DEFAULT 0 CHECK(cpu_millis>=0);
ALTER TABLE coding_deployments ADD COLUMN memory_mib BIGINT NOT NULL DEFAULT 0 CHECK(memory_mib>=0);
ALTER TABLE coding_deployments ADD COLUMN storage_mib BIGINT NOT NULL DEFAULT 0 CHECK(storage_mib>=0);
ALTER TABLE coding_deployments ADD COLUMN capacity_released_at TIMESTAMPTZ;
ALTER TABLE coding_deployments ADD CONSTRAINT deployment_funding_required
    CHECK(reservation_id IS NOT NULL OR capacity_pool_id IS NOT NULL);
ALTER TABLE coding_deployments ADD CONSTRAINT deployment_capacity_shape
    CHECK((capacity_pool_id IS NULL AND cpu_millis=0 AND memory_mib=0 AND storage_mib=0)
       OR (capacity_pool_id IS NOT NULL AND cpu_millis>0 AND memory_mib>0 AND storage_mib>0));
ALTER TABLE coding_deployments ADD CONSTRAINT deployment_capacity_project_fk
    FOREIGN KEY(capacity_pool_id,project_id) REFERENCES coding_host_capacity(pool_id,project_id);
"""


def upgrade():
    op.execute(SCHEMA_SQL)


def downgrade():
    # Deliberately fail rather than erase covered deployments whose funding is shared.
    op.execute("ALTER TABLE coding_deployments ALTER COLUMN reservation_id SET NOT NULL;")
    op.execute("""
    ALTER TABLE coding_deployments DROP CONSTRAINT deployment_funding_required;
    ALTER TABLE coding_deployments DROP CONSTRAINT deployment_capacity_shape;
    ALTER TABLE coding_deployments DROP CONSTRAINT deployment_capacity_project_fk;
    ALTER TABLE coding_deployments DROP COLUMN capacity_pool_id, DROP COLUMN cpu_millis,
        DROP COLUMN memory_mib, DROP COLUMN storage_mib, DROP COLUMN capacity_released_at;
    DROP TABLE coding_host_capacity;
    """)
