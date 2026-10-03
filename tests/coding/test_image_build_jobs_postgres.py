"""Real database authority/concurrency proofs, not a live image-worker attestation."""
import asyncio
import hashlib
import importlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from app.coding import image_build_jobs as jobs
from app.coding import cost_ledger, host_capacity
from app.coding import deployment_admission as deploy
from app.coding import validation_admission as validation
from tests.coding.test_validation_artifact_controller_postgres import (
    exported as exported_fixture, context as context_fixture, covered as covered_fixture,
    prepared as prepared_fixture,
)
from tests.coding.test_host_capacity_postgres import queue
from tests.coding.test_oci_packager import case, base
from app.coding.oci_packager import package_validated_artifact

exported = exported_fixture
context = context_fixture
covered = covered_fixture
prepared = prepared_fixture


@pytest_asyncio.fixture
async def build(exported):
    pool, original, host, claim_id, _ = exported
    runtime = base()
    async with pool.acquire() as conn:
        from app.coding import validation_controller
        row = await conn.fetchrow("SELECT * FROM coding_validation_claims WHERE id=$1", claim_id)
        reference = validation_controller._outcome(row)["result"]["build_artifact"]
    now = datetime.now(timezone.utc)
    request = jobs.ImageBuildRequest(
        run_id=original.run_id, validation_claim_id=claim_id, target_id=original.target_id,
        target_hash=original.target_hash, source_hash=original.source_hash,
        artifact_sha256=reference["archive_sha256"], profile="react_build",
        base_manifest_digest=runtime.approved_manifest_digest, runtime_policy_hash="e" * 64,
        timeout_seconds=60, planned_end_at=now + timedelta(minutes=8),
        output_expires_at=now + timedelta(minutes=10),
    )
    return pool, request, host, reference


async def reserve(build, **changes):
    values = {"user_id": "alice", "idempotency_key": "image-one", "lease_owner": "worker-one"}
    return await jobs.reserve(build[0], build[1], **{**values, **changes})


async def dispatch(build, row, **changes):
    values = {"user_id": "alice", "lease_owner": "worker-one"}
    return await jobs.claim_dispatch(build[0], row["id"], **{**values, **changes})


def receipt(build, issued, **changes):
    _, req, _, reference = build
    body, _, _ = case()
    image = package_validated_artifact(
        base(), body, reference, profile=req.profile, source_hash=req.source_hash,
        claim_id=str(req.validation_claim_id), job_id=reference["validation_job_id"],
    )
    layout = image.layout_archive()
    evidence = image.evidence
    values = {key: evidence[key] for key in (
        "source_hash", "validation_job_id", "profile", "artifact_sha256", "base_manifest_digest",
        "image_manifest_digest", "image_config_digest", "output_layer_digest", "generated_code_executed",
    )}
    values.update(build_job_id=issued["build_job_id"], dispatch_token=issued["dispatch_token"],
                  request_hash=issued["request_hash"], validation_claim_id=req.validation_claim_id,
                  output_archive_sha256=hashlib.sha256(layout).hexdigest(),
                  output_archive_bytes=len(layout), output_expires_at=req.output_expires_at,
                  completed_at=datetime.now(timezone.utc),
                  execution_cleanup_verified=True)
    return jobs.ImageBuildResult.model_validate({**values, **changes})


@pytest.mark.asyncio
async def test_duplicate_reserve_and_claim_dispatch_once(build):
    rows = await asyncio.gather(*[reserve(build) for _ in range(8)])
    assert len({row["id"] for row in rows}) == 1
    issued = await asyncio.gather(*[dispatch(build, rows[0]) for _ in range(8)])
    assert len([item for item in issued if item]) == 1
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_image_build_events "
                                   "WHERE event_type='dispatch_intent'") == 1
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 1


@pytest.mark.asyncio
async def test_idempotency_cannot_change_base_policy_source_or_deadline(build):
    await reserve(build)
    for field, value in (("runtime_policy_hash", "f" * 64), ("source_hash", "d" * 64),
                         ("base_manifest_digest", "sha256:" + "e" * 64),
                         ("planned_end_at", build[1].planned_end_at + timedelta(seconds=1))):
        altered = (build[0], build[1].model_copy(update={field: value}), *build[2:])
        with pytest.raises(jobs.ImageBuildConflict, match="idempotency"):
            await reserve(altered)


@pytest.mark.asyncio
async def test_exact_intent_cannot_be_enqueued_again_under_another_key(build):
    await reserve(build)
    with pytest.raises(jobs.ImageBuildConflict, match="another idempotency key"):
        await reserve(build, idempotency_key="alias-one")
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_image_build_jobs") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tenant", "lease", "source", "artifact", "expired", "failed"])
async def test_authority_and_passing_evidence_required(build, change):
    if change in {"tenant", "lease"}:
        with pytest.raises((deploy.DeploymentConflict, validation.ValidationConflict)):
            await reserve(build, **({"user_id": "mallory"} if change == "tenant"
                                    else {"lease_owner": "other-worker"}))
        return
    pool, req, *_ = build
    if change in {"source", "artifact"}:
        field = "source_hash" if change == "source" else "artifact_sha256"
        build = (pool, req.model_copy(update={field: "d" * 64}), *build[2:])
    else:
        async with pool.acquire() as conn:
            if change == "expired":
                await conn.execute("UPDATE coding_validation_claims SET result_expires_at="
                                   "now()-interval '1 second',result_recorded_at=now()-interval '1 hour'")
            else:
                await conn.execute("UPDATE coding_validation_claims SET validation_outcome='failed'")
    with pytest.raises(jobs.ImageBuildConflict):
        await reserve(build)


@pytest.mark.asyncio
async def test_shared_build_deploy_and_validation_capacity(build, covered):
    first = await reserve(build)
    deployed = await queue(covered)
    with pytest.raises(cost_ledger.CostAdmissionDenied, match="cpu_millis"):
        await queue(covered)
    req = build[1]
    validation_req = validation.ValidationRequest(
        run_id=req.run_id, sequence_no=2, target_id=req.target_id, target_hash=req.target_hash,
        source_hash=req.source_hash, image_id="sha256:" + "a" * 64, profile="python_compile",
        timeout_seconds=10, planned_end_at=req.planned_end_at,
    )
    with pytest.raises(cost_ledger.CostAdmissionDenied, match="cpu_millis"):
        await validation.reserve(build[0], validation_req, user_id="alice", idempotency_key="other-validation")
    assert first["status"] == deployed["status"] == "reserved"


@pytest.mark.asyncio
async def test_finished_result_is_encrypted_immutable_and_frees_cpu_not_output_storage(build, covered):
    row = await reserve(build)
    issued = await dispatch(build, row)
    result = receipt(build, issued)
    saved = await jobs.record_result(build[0], row["id"], user_id="alice", lease_owner="worker-one", result=result)
    assert saved["status"] == "finished" and saved["execution_released_at"]
    assert saved["storage_released_at"] is None
    assert result.output_archive_sha256 not in saved["encrypted_result"]
    assert await jobs.read_result(build[0], row["id"], user_id="alice") == result
    assert (await jobs.record_result(build[0], row["id"], user_id="alice",
                                    lease_owner="worker-one", result=result))["id"] == row["id"]
    with pytest.raises(jobs.ImageBuildConflict, match="immutable"):
        await jobs.record_result(build[0], row["id"], user_id="alice", lease_owner="worker-one",
                                 result=result.model_copy(update={"output_archive_sha256": "f" * 64}))
    # The CPU slot is free, but retained image storage is still charged.
    cap = covered[2].capacity.model_copy(update={"cpu_millis": 2000, "storage_mib": covered[4].storage_mib})
    with pytest.raises(cost_ledger.CostAdmissionDenied, match="storage_mib"):
        await queue(covered, capacity=cap)
    assert (await queue(covered, capacity=cap.model_copy(update={"storage_mib": 1024})))["status"] == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("source_hash", "f" * 64), ("validation_claim_id", uuid4()), ("build_job_id", uuid4()),
    ("dispatch_token", uuid4()), ("validation_job_id", "f" * 32), ("request_hash", "f" * 64),
    ("artifact_sha256", "f" * 64), ("base_manifest_digest", "sha256:" + "f" * 64),
])
async def test_unbound_measurement_does_not_release_capacity(build, field, value):
    row = await reserve(build)
    issued = await dispatch(build, row)
    with pytest.raises(jobs.ImageBuildConflict):
        await jobs.record_result(build[0], row["id"], user_id="alice", lease_owner="worker-one",
                                 result=receipt(build, issued, **{field: value}))
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT execution_released_at IS NULL FROM coding_image_build_jobs")


@pytest.mark.asyncio
async def test_timeout_does_not_reassign_or_release_uncertain_work(build, covered):
    row = await reserve(build)
    issued = await dispatch(build, row)
    await jobs.mark_uncertain(build[0], row["id"], user_id="alice",
                              dispatch_token=issued["dispatch_token"], lease_owner="worker-one")
    async with build[0].acquire() as conn:
        await conn.execute("UPDATE coding_image_build_jobs SET heartbeat_at=now()-interval '1 hour'")
    assert await dispatch(build, row) is None
    cap = covered[2].capacity.model_copy(update={"cpu_millis": 2000})
    with pytest.raises(cost_ledger.CostAdmissionDenied):
        await queue(covered, capacity=cap)
    with pytest.raises(jobs.ImageBuildConflict, match="another image dispatch"):
        await jobs.release_resources(build[0], row["id"], user_id="alice", evidence_hash="a" * 64)
    await jobs.release_resources(build[0], row["id"], user_id="alice", evidence_hash="a" * 64,
                                 dispatch_token=issued["dispatch_token"])
    assert (await queue(covered, capacity=cap))["status"] == "reserved"
    assert await dispatch(build, row) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "lease", "delete", "funding"])
async def test_dispatch_rechecks_authority(build, change):
    row = await reserve(build)
    statements = {
        "revoke": "UPDATE coding_deployment_targets SET status='revoked',revoked_at=now()",
        "lease": "UPDATE coding_runs SET lease_expires_at=now()-interval '1 second'",
        "delete": "UPDATE coding_runs SET deleted_at=now()",
        "funding": "UPDATE cloud_cost_reservations SET status='uncertain'",
    }
    async with build[0].acquire() as conn:
        await conn.execute(statements[change])
    with pytest.raises((deploy.DeploymentConflict, validation.ValidationConflict, cost_ledger.CostAdmissionDenied)):
        await dispatch(build, row)
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT dispatch_token IS NULL FROM coding_image_build_jobs")


@pytest.mark.asyncio
async def test_deleted_run_keeps_cleanup_obligation_but_scrubs_private_output(build):
    row = await reserve(build)
    issued = await dispatch(build, row)
    await jobs.record_result(build[0], row["id"], user_id="alice", lease_owner="worker-one",
                             result=receipt(build, issued))
    async with build[0].acquire() as conn:
        # Validation and build references preserve orphan resource ownership.
        await conn.execute("DELETE FROM coding_runs WHERE id=$1", build[1].run_id)
        stored = await conn.fetchrow("SELECT * FROM coding_image_build_jobs")
        assert stored["run_id"] is None and stored["encrypted_result"] is None
        assert stored["storage_released_at"] is None and stored["result_hash"]
    with pytest.raises(jobs.ImageBuildConflict):
        await jobs.read_result(build[0], row["id"], user_id="alice")
    await jobs.release_resources(build[0], row["id"], user_id="alice", evidence_hash="a" * 64,
                                 dispatch_token=issued["dispatch_token"])


@pytest.mark.asyncio
async def test_soft_delete_purges_private_results_without_releasing_storage(build):
    row = await reserve(build)
    issued = await dispatch(build, row)
    await jobs.record_result(build[0], row["id"], user_id="alice", lease_owner="worker-one",
                             result=receipt(build, issued))
    async with build[0].acquire() as conn:
        await conn.execute("UPDATE coding_runs SET deleted_at=now()")
    assert await jobs.purge_private_results(build[0]) == 1
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT storage_released_at IS NULL FROM coding_image_build_jobs")


@pytest.mark.asyncio
async def test_migration_downgrade_refuses_live_obligations(build, monkeypatch):
    row = await reserve(build)
    migration = importlib.import_module("migrations.versions.021_image_build_jobs")
    statements = []

    class Capture:
        @staticmethod
        def execute(sql):
            statements.append(sql)

    monkeypatch.setattr(migration, "op", Capture)
    migration.downgrade()
    async with build[0].acquire() as conn:
        with pytest.raises(Exception, match="reconcile image resources"):
            await conn.execute(statements[0])
    await jobs.release_resources(build[0], row["id"], user_id="alice", evidence_hash="a" * 64)
    async with build[0].acquire() as conn:
        await conn.execute(statements[0])
        await conn.execute(migration.SCHEMA_SQL)


@pytest.mark.asyncio
async def test_event_failure_rolls_back_claim_and_capacity(build, monkeypatch):
    async def fail(*args):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(jobs, "_event", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await reserve(build)
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_image_build_jobs") == 0


@pytest.mark.asyncio
async def test_heartbeat_bound_to_live_worker_and_does_not_extend_deadline(build):
    row = await reserve(build)
    issued = await dispatch(build, row)
    await jobs.heartbeat(build[0], row["id"], user_id="alice",
                         dispatch_token=issued["dispatch_token"], lease_owner="worker-one")
    with pytest.raises(jobs.ImageBuildConflict, match="identity"):
        await jobs.heartbeat(build[0], row["id"], user_id="alice",
                             dispatch_token=uuid4(), lease_owner="worker-one")
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT planned_end_at FROM coding_image_build_jobs") == build[1].planned_end_at


@pytest.mark.asyncio
async def test_cleanup_possible_after_target_revocation(build):
    row = await reserve(build)
    issued = await dispatch(build, row)
    await deploy.revoke_target(build[0], build[1].target_id, user_id="alice")
    assert (await jobs.release_resources(build[0], row["id"], user_id="alice", evidence_hash="a" * 64,
                                        dispatch_token=issued["dispatch_token"]))["status"] == "retired"


@pytest.mark.asyncio
async def test_new_controller_recovers_old_receipt_without_second_dispatch(build, monkeypatch):
    row = await reserve(build)
    issued = await dispatch(build, row)
    result = receipt(build, issued)
    await jobs.mark_uncertain(build[0], row["id"], user_id="alice",
                              dispatch_token=issued["dispatch_token"], lease_owner="worker-one")
    async with build[0].acquire() as conn:
        await conn.execute("UPDATE coding_runs SET lease_owner='recovery-worker',"
                           "lease_expires_at=now()+interval '10 minutes'")
    assert await dispatch(build, row, lease_owner="recovery-worker") is None
    # Receipt was measured within the original authority interval; receipt delivery
    # after that interval is recovery, not an extension of execution authority.
    class AfterExecutionDeadline(datetime):
        @classmethod
        def now(cls, tz=None):
            return build[1].planned_end_at + timedelta(seconds=1)

    monkeypatch.setattr(jobs, "datetime", AfterExecutionDeadline)
    saved = await jobs.record_result(build[0], row["id"], user_id="alice",
                                     lease_owner="recovery-worker", result=result)
    assert saved["status"] == "finished" and saved["worker_owner"] == "worker-one"
    assert await jobs.read_result(build[0], row["id"], user_id="alice") == result


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["before_dispatch", "after_deadline", "future"])
async def test_invalid_worker_completion_time_cannot_record_success(build, where):
    row = await reserve(build)
    issued = await dispatch(build, row)
    times = {"before_dispatch": row["created_at"] - timedelta(seconds=1),
             "after_deadline": build[1].planned_end_at + timedelta(seconds=1),
             "future": datetime.now(timezone.utc) + timedelta(minutes=1)}
    with pytest.raises(jobs.ImageBuildConflict, match="authority"):
        await jobs.record_result(build[0], row["id"], user_id="alice", lease_owner="worker-one",
                                 result=receipt(build, issued, completed_at=times[where]))


@pytest.mark.asyncio
async def test_changed_target_config_denied_even_if_stored_hash_unchanged(build):
    row = await reserve(build)
    async with build[0].acquire() as conn:
        await conn.execute("UPDATE coding_deployment_targets SET config="
                           "jsonb_set(config,'{server_uuid}','\"other-server\"'::jsonb)")
    with pytest.raises(jobs.ImageBuildConflict, match="target identity"):
        await dispatch(build, row)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["insufficient_execution", "retention_before_execution", "retention_too_long"])
async def test_deadline_bounds_before_capacity_reservation(build, change):
    now = datetime.now(timezone.utc)
    altered = {"insufficient_execution": {"planned_end_at": now + timedelta(seconds=10)},
               "retention_before_execution": {"output_expires_at": now + timedelta(seconds=10)},
               "retention_too_long": {"output_expires_at": now + timedelta(hours=25)}}[change]
    with pytest.raises(jobs.ImageBuildConflict, match="deadlines"):
        await reserve((build[0], build[1].model_copy(update=altered), *build[2:]))
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_image_build_jobs") == 0


@pytest.mark.asyncio
async def test_event_failure_rolls_back_dispatch_token(build, monkeypatch):
    row = await reserve(build)

    async def fail(*args):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(jobs, "_event", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await dispatch(build, row)
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM coding_image_build_jobs") == "reserved"
        assert await conn.fetchval("SELECT dispatch_token IS NULL FROM coding_image_build_jobs")
