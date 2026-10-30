"""Internal image-packager job authority, not a model tool or deployment grant.

Only authenticated controllers call these functions. A receipt must come from a
separate keyless, resource-enforced worker after measuring its durable output and
verifying process cleanup. This ledger does not itself run that worker, verify a
filesystem object, publish an image, sign provenance, or deploy an application.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.coding import cost_ledger, deployment_admission as deployment, host_capacity
from app.coding import validation_admission as validation
from app.coding import validation_controller
from app.coding.repository import encrypt_json, decrypt_json


class ImageBuildConflict(RuntimeError):
    pass


class ImageBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    validation_claim_id: UUID
    target_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: Literal["react_build", "spring_package"]
    base_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeout_seconds: int = Field(ge=1, le=900, strict=True)
    planned_end_at: datetime
    output_expires_at: datetime

    @field_validator("planned_end_at", "output_expires_at")
    @classmethod
    def zoned(cls, value):
        if value.utcoffset() is None:
            raise ValueError("image deadlines must include timezone")
        return value.astimezone(timezone.utc)


class ImageBuildResult(BaseModel):
    """Bound measurement receipt; validation is not proof of its trusted origin."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    build_job_id: UUID
    dispatch_token: UUID
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_claim_id: UUID
    validation_job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    profile: Literal["react_build", "spring_package"]
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    image_config_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_layer_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_archive_bytes: int = Field(gt=0, le=768 * 1024 * 1024, strict=True)
    output_expires_at: datetime
    completed_at: datetime
    generated_code_executed: Literal[False]
    execution_cleanup_verified: Literal[True]

    @field_validator("output_expires_at", "completed_at")
    @classmethod
    def zoned(cls, value):
        return ImageBuildRequest.zoned(value)


async def _event(conn, job_id, kind):
    await conn.execute("INSERT INTO coding_image_build_events(job_id,event_type) VALUES($1,$2)",
                       job_id, kind)


def _capacity(target):
    if not target.capacity_pool_id:
        raise ImageBuildConflict("image packaging requires a funded execution host")
    return host_capacity.CapacityRequest(pool_id=target.capacity_pool_id, cpu_millis=1000,
                                         memory_mib=3072, storage_mib=2048)


def _target_config(row, request):
    target = deployment.RegisteredTarget.model_validate(deployment._json(row["config"]))
    if row["config_hash"] != request.target_hash or deployment.fingerprint(target) != request.target_hash:
        raise ImageBuildConflict("image target identity changed")
    return target


async def _evidence(conn, request, user_id):
    row = await conn.fetchrow(
        "SELECT * FROM coding_validation_claims WHERE id=$1 AND user_id=$2 FOR SHARE",
        request.validation_claim_id, user_id,
    )
    if not row or row["run_id"] != request.run_id or row["status"] != "released":
        raise ImageBuildConflict("passing build belongs to another run or is unavailable")
    original = validation.ValidationRequest.model_validate(deployment._json(row["request"]))
    if (row["validation_outcome"] != "passed" or original.target_id != request.target_id
            or original.target_hash != request.target_hash or original.source_hash != request.source_hash
            or original.profile != request.profile
            or deployment.fingerprint(original) != row["request_hash"]):
        raise ImageBuildConflict("passing build evidence does not match image request")
    result = validation_controller._outcome(row)["result"]
    if result is None:
        raise ImageBuildConflict("private build evidence has expired")
    verified = validation_controller._verified_result(original, row["id"], result)
    artifact = verified["build_artifact"]
    if artifact["archive_sha256"] != request.artifact_sha256:
        raise ImageBuildConflict("image request refers to another build artifact")
    return artifact


async def _admit(conn, request, target, *, exclude=None):
    now = datetime.now(timezone.utc)
    if (not now + timedelta(seconds=request.timeout_seconds + 90) <= request.planned_end_at
            <= now + timedelta(seconds=1920)
            or not request.planned_end_at <= request.output_expires_at <= now + timedelta(hours=24)):
        raise ImageBuildConflict("image execution/retention deadlines are invalid")
    return await host_capacity.admit(
        conn, _capacity(target), project_id=target.project_id, server_uuid=target.server_uuid,
        planned_end_at=request.output_expires_at, exclude_image_build=exclude,
    )


def _request(row, *, cleanup=False):
    request = ImageBuildRequest.model_validate(deployment._json(row["request"]))
    if (deployment.fingerprint(request) != row["request_hash"]
            or (row["run_id"] != request.run_id and not (cleanup and row["run_id"] is None))
            or row["target_id"] != request.target_id
            or row["validation_claim_id"] != request.validation_claim_id
            or row["planned_end_at"] != request.planned_end_at
            or row["output_expires_at"] != request.output_expires_at):
        raise ImageBuildConflict("immutable image-build request changed or was deleted")
    return request


def _worker_binding(row, request, target, validation_job_id, token, *, cleanup=False):
    from app.coding.image_output_store import verify_binding
    return verify_binding({
        "version": 1, "build_job_id": str(row["id"]), "dispatch_token": str(token),
        "request_hash": row["request_hash"], "source_hash": request.source_hash,
        "validation_claim_id": str(request.validation_claim_id),
        "validation_job_id": validation_job_id, "profile": request.profile,
        "artifact_sha256": request.artifact_sha256, "base_manifest_digest": request.base_manifest_digest,
        "runtime_policy_hash": request.runtime_policy_hash, "timeout_seconds": request.timeout_seconds,
        "execution_deadline_unix": int(request.planned_end_at.timestamp()),
        "output_expires_at_unix": int(request.output_expires_at.timestamp()),
        "target_hash": request.target_hash, "server_uuid": target.server_uuid,
        "project_id": target.project_id,
    }, allow_expired=cleanup)


async def _owned(conn, job_id, user_id, *, cleanup=False):
    row = await conn.fetchrow("SELECT * FROM coding_image_build_jobs WHERE id=$1 AND user_id=$2",
                              job_id, user_id)
    if not row:
        raise ImageBuildConflict("image job unavailable for this user")
    target = await deployment._target(conn, row["target_id"], user_id, allow_revoked=cleanup)
    row = await conn.fetchrow("SELECT * FROM coding_image_build_jobs WHERE id=$1 FOR UPDATE", job_id)
    return row, target


async def reserve(pool, request: ImageBuildRequest, *, user_id, idempotency_key, lease_owner):
    request = ImageBuildRequest.model_validate(request.model_dump())
    cost_ledger._text(user_id)
    cost_ledger._text(idempotency_key)
    cost_ledger._text(lease_owner)
    digest = deployment.fingerprint(request)
    lock = int.from_bytes(hashlib.sha256(json.dumps(
        ["image-build", user_id, idempotency_key],
    ).encode()).digest()[:8], "big", signed=True)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", lock)
        target_row = await deployment._target(conn, request.target_id, user_id)
        target = _target_config(target_row, request)
        await validation._run(conn, request, user_id, lease_owner)
        old = await conn.fetchrow(
            "SELECT * FROM coding_image_build_jobs WHERE user_id=$1 AND idempotency_key=$2",
            user_id, idempotency_key,
        )
        if old:
            if old["request_hash"] != digest:
                raise ImageBuildConflict("idempotency key belongs to another image build")
            _request(old)
            return dict(old)
        duplicate = await conn.fetchval(
            "SELECT id FROM coding_image_build_jobs WHERE validation_claim_id=$1 AND request_hash=$2",
            request.validation_claim_id, digest,
        )
        if duplicate:
            raise ImageBuildConflict("exact image build already has another idempotency key")
        await _evidence(conn, request, user_id)
        await _admit(conn, request, target)
        cap = _capacity(target)
        row = await conn.fetchrow(
            """INSERT INTO coding_image_build_jobs
            (id,run_id,user_id,validation_claim_id,target_id,project_id,pool_id,request,request_hash,
             idempotency_key,cpu_millis,memory_mib,storage_mib,planned_end_at,output_expires_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,$14,$15) RETURNING *""",
            uuid4(), request.run_id, user_id, request.validation_claim_id, request.target_id,
            target.project_id, cap.pool_id, request.model_dump_json(), digest, idempotency_key,
            cap.cpu_millis, cap.memory_mib, cap.storage_mib,
            request.planned_end_at, request.output_expires_at,
        )
        await _event(conn, row["id"], "reserved")
        return dict(row)


async def claim_dispatch(pool, job_id, *, user_id, lease_owner):
    cost_ledger._text(lease_owner)
    async with pool.acquire() as conn, conn.transaction():
        row, target_row = await _owned(conn, job_id, user_id)
        if row["status"] != "reserved":
            return None  # Never reassign a consumed dispatch after heartbeat/lease expiry.
        request = _request(row)
        target = _target_config(target_row, request)
        if row["project_id"] != target.project_id or row["pool_id"] != target.capacity_pool_id:
            raise ImageBuildConflict("image job host identity changed")
        await validation._run(conn, request, user_id, lease_owner)
        artifact = await _evidence(conn, request, user_id)
        await _admit(conn, request, target, exclude=job_id)
        token = uuid4()
        await conn.execute(
            "UPDATE coding_image_build_jobs SET status='dispatched',dispatch_token=$2,worker_owner=$3,"
            "dispatched_at=now(),heartbeat_at=now(),updated_at=now() WHERE id=$1", job_id, token, lease_owner,
        )
        await _event(conn, job_id, "dispatch_intent")
        binding = _worker_binding(row, request, target, artifact["validation_job_id"], token)
        return {"build_job_id": job_id, "dispatch_token": token, "request_hash": row["request_hash"],
                "request": request, "artifact": artifact, "server_uuid": target.server_uuid,
                "worker_binding": binding}


def _dispatch(row, token, owner):
    if row["dispatch_token"] != token or row["worker_owner"] != owner:
        raise ImageBuildConflict("image worker dispatch identity changed")


async def reconcile_execution_observation(pool, job_id, *, user_id, observation):
    """Internal authenticated HOST-journal evidence, not a guest/model assertion.

    The caller must pin/authenticate the transport to the registered server. Exact
    fingerprints prevent cross-job replay; they do not authenticate the caller.
    Process termination alone never proves packaging success or output removal.
    Recovery remains possible after run deletion/revocation/retention expiry.
    """
    if not isinstance(observation, dict):
        raise ImageBuildConflict("invalid image execution observation")
    async with pool.acquire() as conn, conn.transaction():
        row, target_row = await _owned(conn, job_id, user_id, cleanup=True)
        request = _request(row, cleanup=True)
        target = _target_config(target_row, request)
        if row["project_id"] != target.project_id or row["pool_id"] != target.capacity_pool_id:
            raise ImageBuildConflict("image job host identity changed")
        if row["dispatch_token"] is None:
            raise ImageBuildConflict("undispatched image job has no host observation")
        if observation == {"claim_id": str(job_id), "state": "unknown", "cleanup_verified": False}:
            return dict(row)  # Not-found cannot free capacity or authorize redispatch.
        required = {"claim_id", "packaging_job_id", "purpose", "request_hash", "binding_hash",
                    "state", "cleanup_verified", "packaging_success"}
        state = observation.get("state")
        host_job_id = observation.get("packaging_job_id")
        if (set(observation) != required or observation.get("claim_id") != str(job_id)
                or observation.get("purpose") != "image_packaging"
                or observation.get("request_hash") != row["request_hash"]
                or not isinstance(host_job_id, str) or len(host_job_id) != 32
                or any(c not in "0123456789abcdef" for c in host_job_id)
                or not isinstance(state, str) or state not in {"creating", "running", "cleanup_uncertain", "cleaned"}
                or type(observation["cleanup_verified"]) is not bool
                or observation["cleanup_verified"] != (state == "cleaned")
                or observation["packaging_success"] is not None
                or (row["host_job_id"] and row["host_job_id"] != host_job_id)):
            raise ImageBuildConflict("image host observation binding mismatch")
        validation_job_id = await conn.fetchval(
            "SELECT host_job_id FROM coding_validation_claims WHERE id=$1 AND user_id=$2",
            request.validation_claim_id, user_id,
        )
        expected = _worker_binding(row, request, target, validation_job_id,
                                   row["dispatch_token"], cleanup=True)
        expected_hash = hashlib.sha256(json.dumps(expected, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
        if observation["binding_hash"] != expected_hash:
            raise ImageBuildConflict("image host observation dispatch changed")
        collision = await conn.fetchval(
            "SELECT id FROM coding_image_build_jobs WHERE pool_id=$1 AND host_job_id=$2 AND id<>$3",
            row["pool_id"], host_job_id, job_id,
        )
        if collision:
            raise ImageBuildConflict("host job belongs to another image dispatch")
        digest = hashlib.sha256(json.dumps(observation, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        if (row["host_observation_hash"] == digest
                and (row["execution_released_at"] is not None) == observation["cleanup_verified"]
                and (row["status"] != "retired" or observation["cleanup_verified"])):
            return dict(row)
        updated = await conn.fetchrow(
            "UPDATE coding_image_build_jobs SET host_job_id=$2,host_observation_hash=$3,host_observed_at=now(),"
            "execution_released_at=CASE WHEN $4 THEN COALESCE(execution_released_at,now()) ELSE NULL END,"
            "storage_released_at=CASE WHEN NOT $4 THEN NULL ELSE storage_released_at END,"
            "status=CASE WHEN status='retired' AND $4 THEN 'retired' "
            "WHEN NOT $4 OR result_hash IS NULL THEN 'uncertain' ELSE status END,"
            "updated_at=now() WHERE id=$1 RETURNING *",
            job_id, host_job_id, digest, observation["cleanup_verified"],
        )
        await _event(conn, job_id, "execution_cleanup_observed" if observation["cleanup_verified"]
                     else "execution_presence_observed")
        return dict(updated)


async def recover_execution(pool, job_id, *, user_id, host):
    """Read-only pinned IAP/SSH recovery; never launch or reassign a lost worker.

    Provider failures propagate as transport uncertainty without releasing capacity.
    The remote journal is operator-owned and must be the same host used by dispatch.
    No caller-supplied observation or remote shell command enters this path.
    """
    from app.coding import validation_transport as transport
    async with pool.acquire() as conn, conn.transaction():
        row, target_row = await _owned(conn, job_id, user_id, cleanup=True)
        request = _request(row, cleanup=True)
        target = _target_config(target_row, request)
        if target.server_uuid != host.server_uuid or target.project_id != host.project_id:
            raise ImageBuildConflict("recovery transport is not the registered image host")
        if row["dispatch_token"] is None:
            raise ImageBuildConflict("undispatched image job has no host observation")
    transport.command(host)  # Protected operator credentials and exact SSH pin.
    observation = await transport.observe(host, str(job_id))
    return await reconcile_execution_observation(pool, job_id, user_id=user_id, observation=observation)


async def heartbeat(pool, job_id, *, user_id, dispatch_token, lease_owner):
    async with pool.acquire() as conn, conn.transaction():
        row, target = await _owned(conn, job_id, user_id)
        _dispatch(row, dispatch_token, lease_owner)
        if row["status"] != "dispatched" or row["planned_end_at"] <= datetime.now(timezone.utc):
            raise ImageBuildConflict("image execution authority expired or is not active")
        request = _request(row)
        _target_config(target, request)
        await validation._run(conn, request, user_id, lease_owner)
        await conn.execute("UPDATE coding_image_build_jobs SET heartbeat_at=now(),updated_at=now() "
                           "WHERE id=$1", job_id)


async def record_result(pool, job_id, *, user_id, lease_owner, result: ImageBuildResult):
    """Only for a transport-authenticated, rehashed CAS receipt with verified worker cleanup."""
    result = ImageBuildResult.model_validate(result.model_dump())
    digest = deployment.fingerprint(result)
    async with pool.acquire() as conn, conn.transaction():
        row, target = await _owned(conn, job_id, user_id)
        # A new leased controller may recover the OLD worker's authenticated receipt.
        # This does not mint a second dispatch or change the original worker identity.
        if row["dispatch_token"] != result.dispatch_token:
            raise ImageBuildConflict("image worker dispatch identity changed")
        request = _request(row)
        _target_config(target, request)
        await validation._run(conn, request, user_id, lease_owner)
        if row["result_hash"]:
            if row["result_hash"] != digest:
                raise ImageBuildConflict("image result is immutable")
            if row["execution_released_at"] is None:
                raise ImageBuildConflict("image host execution remains unreconciled")
            if row["status"] == "finished":
                return dict(row)
        now = datetime.now(timezone.utc)
        if (row["status"] not in {"dispatched", "uncertain"} or now >= request.output_expires_at
                or not row["dispatched_at"] <= result.completed_at <= min(now, request.planned_end_at)):
            raise ImageBuildConflict("no live image result authority remains")
        artifact = await _evidence(conn, request, user_id)
        expected = {"build_job_id": job_id, "request_hash": row["request_hash"],
                    "validation_job_id": artifact["validation_job_id"]}
        expected.update({key: getattr(request, key) for key in (
            "source_hash", "validation_claim_id", "profile", "artifact_sha256",
            "base_manifest_digest", "output_expires_at",
        )})
        if any(getattr(result, key) != value for key, value in expected.items()):
            raise ImageBuildConflict("image measurement is not bound to this job")
        updated = await conn.fetchrow(
            "UPDATE coding_image_build_jobs SET status='finished',result_hash=$2,encrypted_result=$3,"
            "result_recorded_at=now(),execution_released_at=now(),updated_at=now() WHERE id=$1 RETURNING *",
            job_id, digest, encrypt_json(result.model_dump(mode="json")),
        )
        await _event(conn, job_id, "result_recorded")
        return dict(updated)


async def read_result(pool, job_id, *, user_id):
    async with pool.acquire() as conn, conn.transaction():
        row, target = await _owned(conn, job_id, user_id)
        request = _request(row)
        _target_config(target, request)
        retained = await conn.fetchval("SELECT 1 FROM coding_runs WHERE id=$1 AND user_id=$2 "
                                       "AND deleted_at IS NULL", row["run_id"], user_id)
        if (not retained or row["status"] != "finished" or not row["encrypted_result"]
                or row["output_expires_at"] <= datetime.now(timezone.utc)):
            raise ImageBuildConflict("private image output unavailable or expired")
        result = ImageBuildResult.model_validate(decrypt_json(row["encrypted_result"]))
        if deployment.fingerprint(result) != row["result_hash"]:
            raise ImageBuildConflict("stored image measurement changed")
        return result


async def mark_uncertain(pool, job_id, *, user_id, dispatch_token, lease_owner):
    async with pool.acquire() as conn, conn.transaction():
        row, _ = await _owned(conn, job_id, user_id, cleanup=True)
        _dispatch(row, dispatch_token, lease_owner)
        if row["status"] == "dispatched":
            await conn.execute("UPDATE coding_image_build_jobs SET status='uncertain',updated_at=now() "
                               "WHERE id=$1", job_id)
            await _event(conn, job_id, "execution_uncertain")


async def release_resources(pool, job_id, *, user_id, evidence_hash, dispatch_token=None):
    """Trusted host/CAS cleanup only; deadline and heartbeat expiry are not proof.

After dispatch the transport observation must authenticate the server, exact job,
dispatch token, terminated execution and removal of its staging/output objects.
This function records that evidence, not a model's assertion of cleanup.
"""
    if (not isinstance(evidence_hash, str) or len(evidence_hash) != 64
            or any(c not in "0123456789abcdef" for c in evidence_hash)):
        raise ImageBuildConflict("cleanup evidence digest required")
    async with pool.acquire() as conn, conn.transaction():
        row, _ = await _owned(conn, job_id, user_id, cleanup=True)
        if row["dispatch_token"] != dispatch_token:
            raise ImageBuildConflict("cleanup belongs to another image dispatch")
        if row["status"] == "retired":
            return dict(row)
        updated = await conn.fetchrow(
            "UPDATE coding_image_build_jobs SET status='retired',cleanup_evidence_hash=$2,"
            "execution_released_at=COALESCE(execution_released_at,now()),storage_released_at=now(),"
            "encrypted_result=NULL,updated_at=now() WHERE id=$1 RETURNING *", job_id, evidence_hash,
        )
        await _event(conn, job_id, "resources_released")
        return dict(updated)


async def purge_private_results(pool):
    """Privacy expiry/deletion does not release execution or disk capacity."""
    async with pool.acquire() as conn:
        status = await conn.execute(
            "UPDATE coding_image_build_jobs j SET encrypted_result=NULL WHERE encrypted_result IS NOT NULL "
            "AND (run_id IS NULL OR output_expires_at<=now() OR EXISTS(SELECT 1 FROM coding_runs r "
            "WHERE r.id=j.run_id AND r.user_id=j.user_id AND r.deleted_at IS NOT NULL))",
        )
    return int(status.rsplit(" ", 1)[-1])
