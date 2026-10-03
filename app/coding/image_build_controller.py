"""Trusted durable image build orchestration, separate from model/API authority.

All construction happens in the registered keyless host gateway. No generated code
or image assembly runs in this controller. A transport timeout, cancellation or
lost response never authorizes another dispatch. Registry/signing are later gates.
"""
import asyncio
import hashlib
from uuid import UUID

from app.coding import image_build_jobs as jobs, image_build_transport as transport
from app.coding import deployment_admission as deployment, validation_admission as validation
from app.coding import validation_transport
from app.coding import oci_packager as oci


async def _context(pool, job_id, *, user_id, host, lease_owner=None, cleanup=False):
    async with pool.acquire() as conn, conn.transaction():
        row, target_row = await jobs._owned(conn, job_id, user_id, cleanup=cleanup)
        request = jobs._request(row, cleanup=cleanup)
        target = jobs._target_config(target_row, request)
        if target.server_uuid != host.server_uuid or target.project_id != host.project_id:
            raise jobs.ImageBuildConflict("image transport is not the registered host")
        if row["dispatch_token"] is None:
            raise jobs.ImageBuildConflict("image job has not been dispatched")
        if not cleanup:
            await validation._run(conn, request, user_id, lease_owner)
            await jobs._evidence(conn, request, user_id)
        original = await conn.fetchrow("SELECT * FROM coding_validation_claims WHERE id=$1 AND user_id=$2",
                                       request.validation_claim_id, user_id)
        if original is None:
            raise jobs.ImageBuildConflict("image validation claim unavailable")
        source_request = validation.ValidationRequest.model_validate(deployment._json(original["request"]))
        if deployment.fingerprint(source_request) != original["request_hash"]:
            raise jobs.ImageBuildConflict("immutable image validation changed")
        binding = jobs._worker_binding(row, request, target, original["host_job_id"],
                                       row["dispatch_token"], cleanup=cleanup)
        envelope = validation._host_envelope(original["id"], source_request, target, original["request_hash"])
        return dict(row), request, binding, envelope


async def _record(pool, row, request, binding, measurement, *, user_id, lease_owner):
    transport.verify_measurement(binding, measurement)
    observation = {"claim_id": str(row["id"]), "packaging_job_id": measurement["packaging_job_id"],
                   "purpose": "image_packaging", "request_hash": row["request_hash"],
                   "binding_hash": measurement["binding_hash"], "state": "cleaned", "cleanup_verified": True,
                   "packaging_success": None}
    # The authenticated root gateway has already rehashed the actual output and
    # terminated its worker. Stopping execution and retaining an image are distinct.
    await jobs.reconcile_execution_observation(pool, row["id"], user_id=user_id, observation=observation)
    evidence = measurement["evidence"]
    result = jobs.ImageBuildResult(
        build_job_id=row["id"], dispatch_token=row["dispatch_token"], request_hash=row["request_hash"],
        source_hash=request.source_hash, validation_claim_id=request.validation_claim_id,
        validation_job_id=binding["validation_job_id"], profile=request.profile,
        artifact_sha256=request.artifact_sha256, base_manifest_digest=request.base_manifest_digest,
        image_manifest_digest=evidence["image_manifest_digest"], image_config_digest=evidence["image_config_digest"],
        output_layer_digest=evidence["output_layer_digest"], output_archive_sha256=measurement["output_archive_sha256"],
        output_archive_bytes=measurement["output_archive_bytes"], output_expires_at=request.output_expires_at,
        completed_at=measurement["completed_at"], generated_code_executed=False, execution_cleanup_verified=True,
    )
    return await jobs.record_result(pool, row["id"], user_id=user_id, lease_owner=lease_owner, result=result)


async def recover(pool, job_id: UUID, *, user_id, host, lease_owner):
    row, request, binding, _ = await _context(pool, job_id, user_id=user_id, host=host, lease_owner=lease_owner)
    if row["result_hash"] and row["status"] == "finished":
        return await jobs.read_result(pool, job_id, user_id=user_id)
    # An unavailable output may still have terminated execution. Observe that via
    # the independent host journal without releasing retained storage or retrying.
    await jobs.recover_execution(pool, job_id, user_id=user_id, host=host)
    measurement = await transport.recover(host, binding)
    await _record(pool, row, request, binding, measurement, user_id=user_id, lease_owner=lease_owner)
    return await jobs.read_result(pool, job_id, user_id=user_id)


async def execute(pool, request: jobs.ImageBuildRequest, *, user_id, host, lease_owner, idempotency_key):
    # Check protected credential paths and exact SSH pin before creating an obligation.
    validation_transport.command(host)
    async with pool.acquire() as conn, conn.transaction():
        target_row = await deployment._target(conn, request.target_id, user_id)
        target = jobs._target_config(target_row, request)
        if target.server_uuid != host.server_uuid or target.project_id != host.project_id:
            raise jobs.ImageBuildConflict("image transport is not the registered host")
    row = await jobs.reserve(pool, request, user_id=user_id, idempotency_key=idempotency_key,
                             lease_owner=lease_owner)
    if row["result_hash"] and row["status"] == "finished":
        return await jobs.read_result(pool, row["id"], user_id=user_id)
    issued = await jobs.claim_dispatch(pool, row["id"], user_id=user_id, lease_owner=lease_owner)
    if issued is None:
        return await recover(pool, row["id"], user_id=user_id, host=host, lease_owner=lease_owner)
    try:
        dispatched, request, binding, validation_binding = await _context(
            pool, row["id"], user_id=user_id, host=host, lease_owner=lease_owner)
        measurement = await transport.assemble(host, binding, validation_binding, issued["artifact"])
        await _record(pool, dispatched, request, binding, measurement, user_id=user_id, lease_owner=lease_owner)
    except asyncio.CancelledError:
        await asyncio.shield(jobs.mark_uncertain(pool, row["id"], user_id=user_id,
                                                dispatch_token=issued["dispatch_token"], lease_owner=lease_owner))
        raise
    except (validation_transport.ValidationTransportError, OSError, jobs.ImageBuildConflict,
            validation.ValidationConflict, deployment.DeploymentConflict):
        await jobs.mark_uncertain(pool, row["id"], user_id=user_id,
                                  dispatch_token=issued["dispatch_token"], lease_owner=lease_owner)
        raise
    return await jobs.read_result(pool, row["id"], user_id=user_id)


async def cleanup(pool, job_id, *, user_id, host):
    """Scope-bound physical removal, including after deletion/revocation/expiry."""
    row, _, binding, _ = await _context(pool, job_id, user_id=user_id, host=host, cleanup=True)
    # Authenticated cleanup proves BOTH worker removal and its private volume disposal.
    removal = await transport.cleanup(host, binding)
    transport.verify_cleanup(binding, removal)
    observation = {"claim_id": str(job_id), "packaging_job_id": removal["packaging_job_id"],
                   "purpose": "image_packaging", "request_hash": row["request_hash"],
                   "binding_hash": removal["binding_hash"], "state": "cleaned", "cleanup_verified": True,
                   "packaging_success": None}
    await jobs.reconcile_execution_observation(pool, job_id, user_id=user_id, observation=observation)
    digest = hashlib.sha256(oci.canonical(removal)).hexdigest()
    return await jobs.release_resources(pool, job_id, user_id=user_id, evidence_hash=digest,
                                        dispatch_token=row["dispatch_token"])
