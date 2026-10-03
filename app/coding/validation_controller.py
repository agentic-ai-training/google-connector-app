"""Durable trusted validation orchestration; not a public API or model tool.

Only the worker supplies run/lease/target and frozen snapshot. A result is evidence
for a fixed validation command, not permission to publish, deploy or claim full success.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import UUID

from app.coding import deployment_admission as deployment
from app.coding import isolated_validation as isolation
from app.coding import validation_admission as admission
from app.coding import validation_transport as transport
from app.coding.build_artifact import BuildArtifactError, verify_artifact_reference
from app.coding.repository import encrypt_json, decrypt_json
from app.config.settings import get_settings


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _verified_result(request, claim_id, result):
    if not isinstance(result, dict):
        raise admission.ValidationConflict("invalid validation result")
    required = {
        "claim_id": str(claim_id), "request_hash": deployment.fingerprint(request),
        "source_hash": request.source_hash, "image_id": request.image_id,
        "profile": request.profile, "network": "none", "isolation": "gvisor",
    }
    if (any(result.get(key) != value for key, value in required.items())
            or result.get("cleanup_verified") is not True
            or result.get("workspace_mutations_discarded") is not True
            or type(result.get("success")) is not bool
            or type(result.get("exit_code")) is not int
            or result["success"] != (result["exit_code"] == 0)
            or not re.fullmatch(r"[a-f0-9]{32}", str(result.get("validation_job_id", "")))):
        raise admission.ValidationConflict("validation result binding mismatch")
    text = {}
    for field in ("stdout", "stderr"):
        value = result.get(field, "")
        if not isinstance(value, str):
            raise admission.ValidationConflict("invalid validation output")
        text[field] = value
    if sum(len(value.encode()) for value in text.values()) > isolation.MAX_OUTPUT_BYTES * 3:
        # UTF-8 replacement of invalid raw bytes may increase the encoded byte count.
        raise admission.ValidationConflict("validation output exceeds bounds")
    artifact = {}
    if result["success"] and request.profile in {"react_build", "spring_package"}:
        try:
            artifact["build_artifact"] = verify_artifact_reference(
                result.get("build_artifact"), profile=request.profile,
                source_hash=request.source_hash, claim_id=str(claim_id),
                job_id=result["validation_job_id"],
            )
        except BuildArtifactError as error:
            raise admission.ValidationConflict("validated build artifact is absent or unbound") from error
    elif result.get("build_artifact") is not None:
        raise admission.ValidationConflict("unexpected validated build artifact")
    return {**required, **text, "success": result["success"], "exit_code": result["exit_code"],
            "cleanup_verified": True, "workspace_mutations_discarded": True,
            "validation_job_id": result["validation_job_id"],
            "output_truncated": result.get("output_truncated") is True, **artifact}


async def _read(pool, claim_id, user_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT c.* FROM coding_validation_claims c JOIN coding_runs r ON r.id=c.run_id "
            "AND r.user_id=c.user_id WHERE c.id=$1 AND c.user_id=$2 AND r.deleted_at IS NULL",
            claim_id, user_id,
        )
    if not row:
        raise admission.ValidationConflict("validation unavailable for this user")
    return dict(row)


def _outcome(row):
    result = None
    if row["encrypted_result"] and row["result_expires_at"] > datetime.now(timezone.utc):
        result = decrypt_json(row["encrypted_result"])
        if _digest(result) != row["result_hash"]:
            raise admission.ValidationConflict("stored validation result digest mismatch")
    return {"claim_id": str(row["id"]), "capacity_status": row["status"],
            "outcome": row["validation_outcome"], "result": result,
            "result_available": result is not None}


async def record_result(pool, claim_id: UUID, *, user_id: str, result: dict):
    """Called only for the identity-verified response of the authenticated transport."""
    async with pool.acquire() as conn, conn.transaction():
        row, _ = await admission._owned(conn, claim_id, user_id, reconcile=True)
        retained = await conn.fetchval(
            "SELECT 1 FROM coding_runs WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL FOR SHARE",
            row["run_id"], user_id,
        )
        if not retained:
            raise admission.ValidationConflict("deleted run cannot retain private validation output")
        request = admission.ValidationRequest.model_validate(deployment._json(row["request"]))
        if deployment.fingerprint(request) != row["request_hash"]:
            raise admission.ValidationConflict("immutable validation request changed")
        verified = _verified_result(request, claim_id, result)
        digest = _digest(verified)
        if row["result_hash"]:
            if row["result_hash"] != digest:
                raise admission.ValidationConflict("validation result is immutable")
            return _outcome(dict(row))
        if row["status"] not in {"dispatched", "uncertain", "released"}:
            raise admission.ValidationConflict("no dispatched validation result expected")
        if row["host_job_id"] and row["host_job_id"] != verified["validation_job_id"]:
            raise admission.ValidationConflict("validation host job identity changed")
        # A released-before-dispatch cancellation cannot acquire a forged result.
        if row["status"] == "released" and not row["host_job_id"]:
            raise admission.ValidationConflict("undispatched validation has no result")
        hours = max(1, min(168, get_settings().private_tool_result_retention_hours))
        expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
        updated = await conn.fetchrow(
            """UPDATE coding_validation_claims SET status='released',released_at=COALESCE(released_at,now()),
            host_job_id=$2,evidence_hash=COALESCE(evidence_hash,$3),validation_outcome=$4,
            result_hash=$3,encrypted_result=$5,result_recorded_at=now(),result_expires_at=$6,
            updated_at=now() WHERE id=$1 RETURNING *""",
            claim_id, verified["validation_job_id"], digest,
            "passed" if verified["success"] else "failed", encrypt_json(verified), expiry,
        )
        await admission._event(conn, claim_id, "result_recorded")
        return _outcome(dict(updated))


async def _host_for_claim(pool, claim_id, user_id, host):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT t.config FROM coding_validation_claims c JOIN coding_deployment_targets t "
            "ON t.target_id=c.target_id WHERE c.id=$1 AND c.user_id=$2", claim_id, user_id,
        )
    if not row:
        raise admission.ValidationConflict("validation unavailable for this user")
    target = deployment.RegisteredTarget.model_validate(deployment._json(row["config"]))
    if target.server_uuid != host.server_uuid or target.project_id != host.project_id:
        raise admission.ValidationConflict("registered host does not match validation target")


async def recover(pool, claim_id: UUID, *, user_id: str, host):
    row = await _read(pool, claim_id, user_id)
    if row["result_hash"] or row["status"] in {"reserved", "released"}:
        return _outcome(row)
    await _host_for_claim(pool, claim_id, user_id, host)
    try:
        observation = await transport.observe(host, str(claim_id))
    except (transport.ValidationTransportError, OSError):
        return _outcome(await _read(pool, claim_id, user_id))
    try:
        await admission.reconcile_host_observation(pool, claim_id, user_id=user_id,
                                                   observation=observation)
    except admission.ValidationConflict:
        # A concurrent authenticated result may have superseded cleanup-only evidence.
        fresh = await _read(pool, claim_id, user_id)
        if not fresh["result_hash"]:
            raise
        return _outcome(fresh)
    return _outcome(await _read(pool, claim_id, user_id))


async def execute(pool, request: admission.ValidationRequest, *, user_id: str,
                  lease_owner: str, idempotency_key: str, host, source: bytes):
    """Single dispatch followed by durable result. Replays recover, never redispatch."""
    if not lease_owner or hashlib.sha256(source).hexdigest() != request.source_hash:
        raise admission.ValidationConflict("worker lease and exact frozen source are required")
    isolation.check_archive(source, request.profile)
    transport.command(host)  # Local credential/path/pin checks before capacity admission.
    async with pool.acquire() as conn, conn.transaction():
        target_row = await deployment._target(conn, request.target_id, user_id)
        target = deployment.RegisteredTarget.model_validate(deployment._json(target_row["config"]))
        if target.server_uuid != host.server_uuid or target.project_id != host.project_id:
            raise admission.ValidationConflict("registered host does not match validation target")
    row = await admission.reserve(pool, request, user_id=user_id, idempotency_key=idempotency_key,
                                  expected_lease_owner=lease_owner)
    if row["result_hash"] or row["status"] == "released":
        return _outcome(row)
    issued = await admission.claim_dispatch(pool, row["id"], user_id=user_id,
                                            expected_lease_owner=lease_owner)
    if issued is None:
        return await recover(pool, row["id"], user_id=user_id, host=host)
    try:
        result = await transport.dispatch(host, issued["host_envelope"], source)
    except (transport.ValidationTransportError, OSError):
        # No new dispatch on a timeout, disconnect, malformed response or spawn error.
        await admission.observe(pool, row["id"], user_id=user_id,
                                evidence_hash=_digest({"transport": "uncertain"}))
        return _outcome(await _read(pool, row["id"], user_id))
    return await record_result(pool, row["id"], user_id=user_id, result=result)


async def purge_expired_results(pool) -> int:
    async with pool.acquire() as conn:
        status = await conn.execute(
            "UPDATE coding_validation_claims c SET encrypted_result=NULL WHERE encrypted_result "
            "IS NOT NULL AND (run_id IS NULL OR result_expires_at<=now() OR EXISTS(SELECT 1 FROM coding_runs r "
            "WHERE r.id=c.run_id AND r.user_id=c.user_id AND r.deleted_at IS NOT NULL))",
        )
    return int(status.rsplit(" ", 1)[-1])
