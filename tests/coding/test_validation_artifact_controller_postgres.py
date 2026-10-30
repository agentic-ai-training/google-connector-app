import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.coding import isolated_validation as isolation
from app.coding import validation_controller as controller
from app.coding import validation_admission as admission
from app.coding import validation_transport as transport
from app.coding import deployment_admission as deployment
from tests.coding.test_isolated_validation import generated_stack
from tests.coding.test_oci_packager import case
from tests.coding.test_runtime_image_store import write_store
from tests.coding.test_validation_controller_postgres import (
    context as context_fixture, covered as covered_fixture, prepared as prepared_fixture,
    result as validation_result,
)

context = context_fixture
covered = covered_fixture
prepared = prepared_fixture


@pytest_asyncio.fixture
async def exported(context, tmp_path, monkeypatch):
    pool, request, host, _ = context
    generated_stack(tmp_path)
    source = isolation.source_archive(tmp_path)
    request = request.model_copy(update={"profile": "react_build",
                                         "source_hash": hashlib.sha256(source).hexdigest()})
    body, reference, _ = case()

    async def dispatch(host_, envelope, source_):
        job_id = "b" * 32
        bound = {**reference, "validation_job_id": job_id,
                 "artifact_id": job_id + "-" + reference["archive_sha256"] + ".tar",
                 "claim_id": envelope["claim_id"], "source_hash": envelope["source_hash"]}
        return validation_result(envelope, build_artifact=bound)

    monkeypatch.setattr(transport, "dispatch", dispatch)
    result = await controller.execute(pool, request, user_id="alice", lease_owner="worker-one",
                                      idempotency_key="export-one", host=host, source=source)
    return pool, request, host, UUID(result["claim_id"]), body


async def fetch(exported, **changes):
    pool, request, host, claim_id, _ = exported
    values = {"user_id": "alice", "host": host, "run_id": request.run_id,
              "target_id": request.target_id, "source_hash": request.source_hash}
    return await controller.fetch_build_artifact(pool, claim_id, **{**values, **changes})


@pytest.mark.asyncio
async def test_authorized_result_retrieves_exact_private_artifact(exported, monkeypatch):
    _, request, _, claim_id, body = exported
    calls = []

    async def read(host, envelope, reference):
        calls.append(reference)
        assert envelope["claim_id"] == str(claim_id)
        assert envelope["source_hash"] == request.source_hash
        return body

    monkeypatch.setattr(transport, "fetch_artifact", read)
    actual, reference = await fetch(exported)
    assert actual == body and reference == calls[0]


@pytest.mark.asyncio
async def test_authorized_artifact_reaches_real_oci_assembly(exported, monkeypatch):
    pool, request, host, claim_id, body = exported

    async def read(*args):
        return body

    monkeypatch.setattr(transport, "fetch_artifact", read)
    with tempfile.TemporaryDirectory(prefix=".gca-controller-packaging-", dir=Path.home()) as folder:
        _, policy, workspace = write_store(Path(folder))
        image = await controller.package_build_artifact(
            pool, claim_id, user_id="alice", host=host, run_id=request.run_id,
            target_id=request.target_id, source_hash=request.source_hash,
            runtime_policy_path=policy, source_workspace=workspace,
        )
    assert image.source_hash == request.source_hash
    assert image.claim_id == str(claim_id)
    assert image.evidence["generated_code_executed"] is False
    assert image.manifest_digest.startswith("sha256:")


@pytest.mark.asyncio
async def test_artifact_expiry_during_assembly_prevents_image_consumption(exported, monkeypatch):
    pool, request, host, claim_id, body = exported
    row = await controller._read(pool, claim_id, "alice")
    expiry = controller._outcome(row)["result"]["build_artifact"]["expires_at_unix"]

    async def read(*args):
        return body

    async def assemble_then_expire(operation):
        image = operation()
        monkeypatch.setattr("app.coding.build_artifact.time.time", lambda: expiry + 1)
        return image

    monkeypatch.setattr(transport, "fetch_artifact", read)
    monkeypatch.setattr(controller.asyncio, "to_thread", assemble_then_expire)
    with tempfile.TemporaryDirectory(prefix=".gca-controller-expiry-", dir=Path.home()) as folder:
        _, policy, workspace = write_store(Path(folder))
        with pytest.raises(admission.ValidationConflict, match="artifact"):
            await controller.package_build_artifact(
                pool, claim_id, user_id="alice", host=host, run_id=request.run_id,
                target_id=request.target_id, source_hash=request.source_hash,
                runtime_policy_path=policy, source_workspace=workspace,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"user_id": "mallory"}, {"run_id": uuid4()}, {"target_id": "another-target"},
    {"source_hash": "f" * 64},
    {"host": SimpleNamespace(server_uuid="another-host", project_id="other-project")},
])
async def test_wrong_identity_never_fetches_build_bytes(exported, monkeypatch, changes):
    async def forbidden(*args):
        raise AssertionError("private transfer must not begin")

    monkeypatch.setattr(transport, "fetch_artifact", forbidden)
    with pytest.raises((admission.ValidationConflict, deployment.DeploymentConflict)):
        await fetch(exported, **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete_user_run", "revoke_target", "expire_result"])
async def test_deletion_or_revocation_during_transfer_prevents_consumption(exported, monkeypatch, change):
    pool, request, _, claim_id, body = exported

    async def read(*args):
        async with pool.acquire() as connection:
            if change == "delete_user_run":
                await connection.execute("UPDATE coding_runs SET deleted_at=now() WHERE id=$1",
                                         request.run_id)
            elif change == "revoke_target":
                await connection.execute("UPDATE coding_deployment_targets SET status='revoked' "
                                         "WHERE target_id=$1", request.target_id)
            else:
                await connection.execute("UPDATE coding_validation_claims SET "
                                         "result_recorded_at=now()-interval '2 minutes', "
                                         "result_expires_at=now()-interval '1 second' WHERE id=$1", claim_id)
        return body

    monkeypatch.setattr(transport, "fetch_artifact", read)
    with pytest.raises((admission.ValidationConflict, deployment.DeploymentConflict)):
        await fetch(exported)
