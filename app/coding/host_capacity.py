"""Trusted host capacity admission. Runtime limit enforcement is a separate prerequisite."""
from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.coding import cost_ledger


class CapacityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pool_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    cpu_millis: int = Field(gt=0, strict=True)
    memory_mib: int = Field(gt=0, strict=True)
    storage_mib: int = Field(gt=0, strict=True)


class FundedHost(CapacityRequest):
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    server_uuid: str = Field(pattern=r"^[a-zA-Z0-9-]{1,128}$")
    funding_reservation_id: UUID
    funding_owner_user_id: str = Field(min_length=1, max_length=512)
    enforcement_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


async def register_pool(pool, host: FundedHost, *, evidence_reference: str):
    """Operator enrollment after verifying host identity, usable limits and their enforcement.

    The parent cost obligation must already cover the host lifetime and ancillary
    allowance. Capacity is the usable portion AFTER control-plane overhead, not total RAM.
    """
    cost_ledger._text(evidence_reference)
    async with pool.acquire() as conn, conn.transaction():
        await cost_ledger._account(conn, host.project_id)
        parent = await cost_ledger._owned(conn, host.project_id, host.funding_reservation_id,
                                         host.funding_owner_user_id)
        if parent["category"] != "hosting" or parent["status"] != "dispatched":
            raise cost_ledger.CostAdmissionDenied("host funding must be an active hosting obligation")
        if parent["planned_end_at"] <= datetime.now(timezone.utc):
            raise cost_ledger.CostAdmissionDenied("host funding has expired")
        await conn.execute(
            """INSERT INTO coding_host_capacity
            (pool_id,project_id,server_uuid,funding_reservation_id,cpu_millis,memory_mib,storage_mib,
             enforcement_hash,evidence_reference)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING""",
            host.pool_id, host.project_id, host.server_uuid, host.funding_reservation_id,
            host.cpu_millis, host.memory_mib, host.storage_mib, host.enforcement_hash, evidence_reference,
        )
        row = await conn.fetchrow("SELECT * FROM coding_host_capacity WHERE pool_id=$1", host.pool_id)
        fields = ("project_id", "server_uuid", "funding_reservation_id", "cpu_millis",
                  "memory_mib", "storage_mib", "enforcement_hash")
        if not row or not row["enabled"] or any(row[key] != getattr(host, key) for key in fields):
            raise cost_ledger.CostLedgerConflict("host identity/capacity is immutable or already allocated")


async def admit(conn, request: CapacityRequest, *, project_id, server_uuid, planned_end_at,
                exclude=None, exclude_validation=None, exclude_image_build=None):
    if not conn.is_in_transaction():
        raise cost_ledger.CostLedgerConflict("capacity admission requires a transaction")
    account = await cost_ledger._account(conn, project_id)
    host = await conn.fetchrow(
        "SELECT * FROM coding_host_capacity WHERE pool_id=$1 AND project_id=$2 FOR UPDATE",
        request.pool_id, project_id,
    )
    if not host or not host["enabled"] or host["server_uuid"] != server_uuid:
        raise cost_ledger.CostAdmissionDenied("host capacity is unavailable for this target")
    funding = await conn.fetchrow("SELECT * FROM cloud_cost_reservations WHERE id=$1",
                                  host["funding_reservation_id"])
    now = datetime.now(timezone.utc)
    if (funding["status"] != "dispatched" or funding["project_id"] != project_id
            or funding["category"] != "hosting" or funding["accrued_paise"] > funding["estimated_paise"]
            or not now < planned_end_at <= min(funding["planned_end_at"], account["credit_expires_at"],
                                              account["hosting_ends_at"])):
        raise cost_ledger.CostAdmissionDenied("host coverage is expired, uncertain or overrun")
    obligations = await conn.fetch(
        """SELECT category,sum(CASE WHEN status='settled' THEN actual_paise
        ELSE GREATEST(estimated_paise,accrued_paise) END) AS amount
        FROM cloud_cost_reservations WHERE project_id=$1 AND status<>'released' GROUP BY category""",
        project_id,
    )
    if any(row["amount"] > {"hosting": 1_400_000, "vertex": 850_000}[row["category"]]
           for row in obligations):
        raise cost_ledger.CostAdmissionDenied("funded account has a recorded envelope overrun")
    used = await conn.fetchrow(
        """SELECT COALESCE(sum(cpu_millis),0) AS cpu_millis,
        COALESCE(sum(memory_mib),0) AS memory_mib, COALESCE(sum(storage_mib),0) AS storage_mib
        FROM coding_deployments WHERE capacity_pool_id=$1 AND capacity_released_at IS NULL
          AND ($2::uuid IS NULL OR id<>$2)""", request.pool_id, exclude,
    )
    validation_used = await conn.fetchrow(
        """SELECT COALESCE(sum(cpu_millis),0) AS cpu_millis,
        COALESCE(sum(memory_mib),0) AS memory_mib, COALESCE(sum(storage_mib),0) AS storage_mib
        FROM coding_validation_claims WHERE pool_id=$1 AND released_at IS NULL
          AND ($2::uuid IS NULL OR id<>$2)""", request.pool_id, exclude_validation,
    )
    image_used = await conn.fetchrow(
        """SELECT COALESCE(sum(CASE WHEN execution_released_at IS NULL THEN cpu_millis ELSE 0 END),0)
          AS cpu_millis,
        COALESCE(sum(CASE WHEN execution_released_at IS NULL THEN memory_mib ELSE 0 END),0) AS memory_mib,
        COALESCE(sum(storage_mib),0) AS storage_mib FROM coding_image_build_jobs
        WHERE pool_id=$1 AND storage_released_at IS NULL AND ($2::uuid IS NULL OR id<>$2)""",
        request.pool_id, exclude_image_build,
    )
    for key in ("cpu_millis", "memory_mib", "storage_mib"):
        if used[key] + validation_used[key] + image_used[key] + getattr(request, key) > host[key]:
            raise cost_ledger.CostAdmissionDenied(f"host_{key}_capacity_exceeded")
    return dict(host)
