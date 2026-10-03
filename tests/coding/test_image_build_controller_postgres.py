"""Real ledger recovery proofs; pinned host responses are simulated, not live."""
import asyncio
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.coding import image_build_controller as controller, image_build_jobs as jobs
from app.coding import image_build_transport as transport, validation_transport
from app.coding import oci_packager as oci, deployment_admission as deployment
from tests.coding.test_image_build_jobs_postgres import (
    build as build_fixture, exported as exported_fixture, context as context_fixture,
    covered as covered_fixture, prepared as prepared_fixture, receipt,
)

build = build_fixture
exported = exported_fixture
context = context_fixture
covered = covered_fixture
prepared = prepared_fixture


@pytest_asyncio.fixture
async def network(build, monkeypatch):
    state = SimpleNamespace(calls=[], binding=None, measurement=None, failure=None)

    async def assemble(host, binding, validation_binding, reference):
        state.calls.append("assemble")
        state.binding = binding
        result = receipt(build, {"build_job_id": build[1].run_id, "dispatch_token": build[1].run_id,
                                 "request_hash": binding["request_hash"]})
        # The actual synthetic OCI bytes are assembled by receipt(); metadata comes
        # from the simulated root gateway, never an HTTP client 'passed:true'.
        evidence = {key: result.model_dump()[key] for key in (
            "profile", "source_hash", "validation_job_id", "artifact_sha256", "base_manifest_digest",
            "image_manifest_digest", "image_config_digest", "output_layer_digest", "generated_code_executed")}
        evidence.update(schema="gca-oci-artifact-build-v1", claim_id=binding["validation_claim_id"])
        state.measurement = dict(build_job_id=binding["build_job_id"], packaging_job_id="c" * 32,
                                 binding_hash=hashlib.sha256(oci.canonical(binding)).hexdigest(),
                                 output_archive_sha256=result.output_archive_sha256,
                                 output_archive_bytes=result.output_archive_bytes,
                                 output_expires_at_unix=binding["output_expires_at_unix"],
                                 completed_at=datetime.now(timezone.utc).isoformat(), evidence=evidence,
                                 execution_cleanup_verified=True)
        assert validation_binding["claim_id"] == binding["validation_claim_id"]
        if state.failure == "timeout":
            raise validation_transport.ValidationTransportError("host_transport_uncertain")
        if state.failure == "cancel":
            raise asyncio.CancelledError
        if state.failure == "wrong_measurement":
            state.measurement["binding_hash"] = "0" * 64
        return state.measurement

    async def recover(host, binding):
        state.calls.append("recover")
        assert binding == state.binding
        return state.measurement

    async def observe(host, job_id):
        state.calls.append("observe")
        return {"claim_id": job_id, "packaging_job_id": "c" * 32, "purpose": "image_packaging",
                "request_hash": state.binding["request_hash"],
                "binding_hash": hashlib.sha256(oci.canonical(state.binding)).hexdigest(),
                "state": "cleaned", "cleanup_verified": True, "packaging_success": None}

    async def cleanup(host, binding):
        state.calls.append("cleanup")
        assert binding == state.binding
        return {"build_job_id": binding["build_job_id"], "packaging_job_id": "c" * 32,
                "binding_hash": hashlib.sha256(oci.canonical(binding)).hexdigest(),
                "execution_cleanup_verified": True, "output_removed": True, "staging_removed": True,
                "observed_at": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr(transport, "assemble", assemble)
    monkeypatch.setattr(transport, "recover", recover)
    monkeypatch.setattr(transport, "cleanup", cleanup)
    monkeypatch.setattr(validation_transport, "observe", observe)
    return state


async def execute(build, **changes):
    return await controller.execute(build[0], build[1], user_id="alice", host=changes.pop("host", build[2]),
                                    lease_owner=changes.pop("lease_owner", "worker-one"),
                                    idempotency_key="packaging-one", **changes)


@pytest.mark.asyncio
async def test_authenticated_measurement_records_once_and_preserves_output_obligation(build, network):
    result = await execute(build)
    assert result.generated_code_executed is False and result.execution_cleanup_verified is True
    assert await execute(build) == result and network.calls == ["assemble"]
    async with build[0].acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM coding_image_build_jobs")
        assert row["status"] == "finished" and row["execution_released_at"]
        assert row["storage_released_at"] is None and row["host_job_id"] == "c" * 32
        assert row["encrypted_result"] and result.output_archive_sha256 not in row["encrypted_result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_lost_response_recovers_original_job_never_redispatches(build, network, failure):
    network.failure = failure
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else validation_transport.ValidationTransportError):
        await execute(build)
    async with build[0].acquire() as conn:
        old = await conn.fetchrow("SELECT * FROM coding_image_build_jobs")
        assert old["status"] == "uncertain" and old["execution_released_at"] is None
        await conn.execute("UPDATE coding_runs SET lease_owner='replacement-worker',"
                           "lease_expires_at=now()+interval '10 minutes'")
    result = await execute(build, lease_owner="replacement-worker")
    assert str(result.build_job_id) == network.binding["build_job_id"]
    assert result.dispatch_token == old["dispatch_token"] and network.calls.count("assemble") == 1
    assert network.calls == ["assemble", "observe", "recover"]


@pytest.mark.asyncio
async def test_wrong_measurement_remains_uncertain_without_success_or_release(build, network):
    network.failure = "wrong_measurement"
    with pytest.raises(validation_transport.ValidationTransportError, match="binding_mismatch"):
        await execute(build)
    async with build[0].acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM coding_image_build_jobs")
        assert row["status"] == "uncertain" and row["result_hash"] is None
        assert row["execution_released_at"] is None and row["storage_released_at"] is None


@pytest.mark.asyncio
async def test_wrong_host_never_reserves_or_dispatches_image_job(build, network):
    with pytest.raises(jobs.ImageBuildConflict, match="registered host"):
        await execute(build, host=SimpleNamespace(server_uuid="other-host", project_id=build[2].project_id))
    assert not network.calls
    async with build[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_image_build_jobs") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("after", ["revoke", "hard_delete", "normal"])
async def test_physical_cleanup_records_both_obligations_after_revocation_or_deletion(build, network, after):
    result = await execute(build)
    if after == "revoke":
        await deployment.revoke_target(build[0], build[1].target_id, user_id="alice")
    elif after == "hard_delete":
        async with build[0].acquire() as conn:
            await conn.execute("DELETE FROM coding_runs WHERE id=$1", build[1].run_id)
    row = await controller.cleanup(build[0], result.build_job_id, user_id="alice", host=build[2])
    assert row["status"] == "retired" and row["execution_released_at"] and row["storage_released_at"]
    assert row["encrypted_result"] is None and network.calls == ["assemble", "cleanup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("output_removed", False), ("staging_removed", False),
                                         ("binding_hash", "0" * 64)])
async def test_invalid_cleanup_receipt_never_releases_storage(build, network, monkeypatch, field, value):
    result = await execute(build)
    real_cleanup = transport.cleanup

    async def invalid(host, binding):
        response = await real_cleanup(host, binding)
        return {**response, field: value}

    monkeypatch.setattr(transport, "cleanup", invalid)
    with pytest.raises(validation_transport.ValidationTransportError, match="cleanup_binding"):
        await controller.cleanup(build[0], result.build_job_id, user_id="alice", host=build[2])
    async with build[0].acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM coding_image_build_jobs")
        assert row["status"] == "finished" and row["storage_released_at"] is None
        assert row["encrypted_result"]
