"""Internal tenant/run-bound validation admission; never a model-callable authority API.

The authenticated orchestrator owns this boundary. Host observations must be verified
by the trusted transport before calling observe(); a model's cleanup claim is not proof.
The API does not mint credentials, deploy code, or promote core candidates.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4
import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.coding import cost_ledger, deployment_admission as deployment, host_capacity


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    sequence_no: int = Field(ge=0, strict=True)
    target_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile: Literal["python_compile", "python_unit", "python_lint", "web_lint", "web_build",
                     "flutter_analyze", "flutter_test", "rust_format", "rust_test"]
    timeout_seconds: int = Field(ge=1, le=1800, strict=True)
    planned_end_at: datetime

    @field_validator("planned_end_at")
    @classmethod
    def zoned(cls, value):
        if value.utcoffset() is None:
            raise ValueError("validation deadline must include a timezone")
        return value.astimezone(timezone.utc)


class ValidationConflict(RuntimeError):
    pass


async def _run(conn, request, user_id):
    row = await conn.fetchrow(
        "SELECT status,source_egress_consented_at,deleted_at FROM coding_runs "
        "WHERE id=$1 AND user_id=$2 FOR SHARE", request.run_id, user_id,
    )
    if (not row or row["deleted_at"] is not None or not row["source_egress_consented_at"]
            or row["status"] not in {"planning", "approved", "executing"}):
        raise ValidationConflict("active consented coding run required for this user")


def _capacity(target):
    if not target.capacity_pool_id:
        raise ValidationConflict("validation requires an already-funded host")
    # Must match the fixed launcher, not a model-selected resource estimate.
    return host_capacity.CapacityRequest(pool_id=target.capacity_pool_id, cpu_millis=1000,
                                         memory_mib=1024, storage_mib=64)


async def _admit(conn, request, target, *, exclude=None):
    now = datetime.now(timezone.utc)
    if not now < request.planned_end_at <= now + timedelta(seconds=1920):
        raise ValidationConflict("validation authority must have a short live deadline")
    if request.planned_end_at < now + timedelta(seconds=request.timeout_seconds + 90):
        raise ValidationConflict("insufficient time for validation and bounded cleanup")
    return await host_capacity.admit(
        conn, _capacity(target), project_id=target.project_id, server_uuid=target.server_uuid,
        planned_end_at=request.planned_end_at, exclude_validation=exclude,
    )


async def _event(conn, claim_id, kind):
    await conn.execute("INSERT INTO coding_validation_events(claim_id,event_type) VALUES($1,$2)",
                       claim_id, kind)


async def reserve(pool, request: ValidationRequest, *, user_id: str, idempotency_key: str):
    cost_ledger._text(user_id)
    cost_ledger._text(idempotency_key)
    digest = deployment.fingerprint(request)
    async with pool.acquire() as conn, conn.transaction():
        lock = int.from_bytes(hashlib.sha256(json.dumps(
            ["validation", user_id, idempotency_key],
        ).encode()).digest()[:8], byteorder="big", signed=True)
        await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", lock)
        target_row = await deployment._target(conn, request.target_id, user_id)
        if target_row["config_hash"] != request.target_hash:
            raise ValidationConflict("validation target identity changed")
        old = await conn.fetchrow(
            "SELECT * FROM coding_validation_claims WHERE user_id=$1 AND idempotency_key=$2",
            user_id, idempotency_key,
        )
        if old:
            if old["request_hash"] != digest:
                raise ValidationConflict("idempotency key belongs to a different validation")
            return dict(old)
        await _run(conn, request, user_id)
        target = deployment.RegisteredTarget.model_validate(deployment._json(target_row["config"]))
        await _admit(conn, request, target)
        cap = _capacity(target)
        row = await conn.fetchrow(
            """INSERT INTO coding_validation_claims
            (id,run_id,user_id,sequence_no,target_id,project_id,pool_id,request,request_hash,
             idempotency_key,cpu_millis,memory_mib,storage_mib,planned_end_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,$14) RETURNING *""",
            uuid4(), request.run_id, user_id, request.sequence_no, request.target_id,
            target.project_id, cap.pool_id, request.model_dump_json(), digest, idempotency_key,
            cap.cpu_millis, cap.memory_mib, cap.storage_mib, request.planned_end_at,
        )
        await _event(conn, row["id"], "reserved")
        return dict(row)


async def _owned(conn, claim_id, user_id, *, reconcile=False):
    row = await conn.fetchrow("SELECT * FROM coding_validation_claims WHERE id=$1 AND user_id=$2",
                              claim_id, user_id)
    if not row:
        raise ValidationConflict("validation unavailable for this user")
    target = await deployment._target(conn, row["target_id"], user_id, allow_revoked=reconcile)
    row = await conn.fetchrow("SELECT * FROM coding_validation_claims WHERE id=$1 FOR UPDATE", claim_id)
    return row, target


async def claim_dispatch(pool, claim_id: UUID, *, user_id: str):
    async with pool.acquire() as conn, conn.transaction():
        row, target_row = await _owned(conn, claim_id, user_id)
        if row["status"] != "reserved":
            return None
        request = ValidationRequest.model_validate(deployment._json(row["request"]))
        if (deployment.fingerprint(request) != row["request_hash"]
                or request.target_hash != target_row["config_hash"]):
            raise ValidationConflict("immutable validation evidence changed")
        await _run(conn, request, user_id)
        target = deployment.RegisteredTarget.model_validate(deployment._json(target_row["config"]))
        if (row["run_id"] != request.run_id or row["sequence_no"] != request.sequence_no
                or row["target_id"] != request.target_id or row["project_id"] != target.project_id
                or row["pool_id"] != target.capacity_pool_id
                or row["planned_end_at"] != request.planned_end_at):
            raise ValidationConflict("validation claim binding changed")
        await _admit(conn, request, target, exclude=claim_id)
        await conn.execute("UPDATE coding_validation_claims SET status='dispatched',updated_at=now() "
                           "WHERE id=$1", claim_id)
        await _event(conn, claim_id, "dispatch_intent")
        return {"claim_id": claim_id, "request": request, "target": target}


async def observe(pool, claim_id: UUID, *, user_id: str, evidence_hash: str,
                  host_job_id: str | None = None, cleanup_verified: bool = False):
    """Trusted authenticated-host reconciliation only, not a client 'passed:true' endpoint."""
    if type(cleanup_verified) is not bool:
        raise ValueError("cleanup_verified must be a boolean from the trusted adapter")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
        raise ValueError("invalid evidence digest")
    if host_job_id is not None and not re.fullmatch(r"[0-9a-f]{32}", host_job_id):
        raise ValueError("invalid host job identity")
    if cleanup_verified and host_job_id is None:
        raise ValidationConflict("dispatched cleanup requires exact host job identity")
    async with pool.acquire() as conn, conn.transaction():
        row, _ = await _owned(conn, claim_id, user_id, reconcile=True)
        if host_job_id is not None and row["host_job_id"] and row["host_job_id"] != host_job_id:
            raise ValidationConflict("host job identity cannot be replaced")
        if row["status"] == "released":
            if not cleanup_verified or row["evidence_hash"] != evidence_hash:
                raise ValidationConflict("cleanup evidence is immutable")
            return dict(row)
        if row["status"] not in {"dispatched", "uncertain"}:
            raise ValidationConflict("no dispatched validation to reconcile")
        state = "released" if cleanup_verified else "uncertain"
        updated = await conn.fetchrow(
            """UPDATE coding_validation_claims SET status=$2,host_job_id=COALESCE(host_job_id,$3),
            evidence_hash=$4,released_at=CASE WHEN $2='released' THEN now() ELSE NULL END,
            updated_at=now() WHERE id=$1 RETURNING *""",
            claim_id, state, host_job_id, evidence_hash,
        )
        await _event(conn, claim_id, state)
        return dict(updated)


async def cancel_before_dispatch(pool, claim_id: UUID, *, user_id: str):
    async with pool.acquire() as conn, conn.transaction():
        row, _ = await _owned(conn, claim_id, user_id, reconcile=True)
        if row["status"] == "released" and row["host_job_id"] is None and row["evidence_hash"] is None:
            return dict(row)
        if row["status"] != "reserved":
            raise ValidationConflict("a dispatched job requires host reconciliation")
        await conn.execute("UPDATE coding_validation_claims SET status='released',released_at=now(),"
                           "updated_at=now() WHERE id=$1", claim_id)
        await _event(conn, claim_id, "cancelled_before_dispatch")
