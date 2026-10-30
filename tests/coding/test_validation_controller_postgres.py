import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from types import SimpleNamespace
import tarfile
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.coding import validation_admission as admission
from app.coding import validation_controller as controller
from app.coding import validation_transport as transport
from app.coding import deployment_admission as deployment
from tests.coding.test_validation_admission_postgres import (
    covered as covered_fixture, prepared as prepared_fixture,
)

covered = covered_fixture
prepared = prepared_fixture


@pytest_asyncio.fixture
async def context(covered, monkeypatch):
    pool, target, *_ = covered
    run_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE coding_runs ADD COLUMN lease_owner TEXT,"
                           "ADD COLUMN lease_expires_at TIMESTAMPTZ")
        await conn.execute("INSERT INTO coding_runs VALUES($1,'alice','executing',now(),NULL,"
                           "'worker-one',now()+interval '10 minutes')", run_id)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        item = tarfile.TarInfo("app/main.py")
        content = b"answer=42\n"
        item.size = len(content)
        archive.addfile(item, io.BytesIO(content))
    source = output.getvalue()
    req = admission.ValidationRequest(
        run_id=run_id, sequence_no=0, target_id=target.target_id,
        target_hash=deployment.fingerprint(target), source_hash=hashlib.sha256(source).hexdigest(),
        image_id="sha256:" + "a" * 64, profile="python_compile", timeout_seconds=10,
        planned_end_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    host = SimpleNamespace(server_uuid=target.server_uuid, project_id=target.project_id)
    monkeypatch.setattr(transport, "command", lambda host: ([], {}))
    monkeypatch.setattr(controller, "get_settings", lambda: SimpleNamespace(
        private_tool_result_retention_hours=24,
    ))
    from app.db import oauth_credentials
    monkeypatch.setattr(oauth_credentials, "get_settings", lambda: SimpleNamespace(
        oauth_encryption_keys="test-validation-result-key", jwt_secret_key="unused-test-key",
    ))
    return pool, req, host, source


def result(envelope, **changes):
    return {"claim_id": envelope["claim_id"], "request_hash": envelope["request_hash"],
            "source_hash": envelope["source_hash"], "image_id": envelope["image_id"],
            "profile": envelope["profile"], "network": "none", "isolation": "gvisor",
            "success": True, "exit_code": 0, "cleanup_verified": True,
            "workspace_mutations_discarded": True, "validation_job_id": "b" * 32,
            "stdout": "private source-dependent result", "stderr": "", **changes}


async def execute(context, **changes):
    pool, req, host, source = context
    values = dict(user_id="alice", lease_owner="worker-one", idempotency_key="validation-one",
                  host=host, source=source)
    return await controller.execute(pool, req, **{**values, **changes})


@pytest.mark.asyncio
async def test_result_is_encrypted_and_replay_never_dispatches(context, monkeypatch):
    calls = []

    async def dispatch(host, envelope, source):
        calls.append(envelope)
        return result(envelope)

    monkeypatch.setattr(transport, "dispatch", dispatch)
    first = await execute(context)
    assert first["outcome"] == "passed" and first["capacity_status"] == "released"
    assert (await execute(context)) == first
    assert len(calls) == 1
    async with context[0].acquire() as conn:
        stored = await conn.fetchrow("SELECT * FROM coding_validation_claims")
        assert "private source" not in stored["encrypted_result"]
        assert stored["result_hash"] and stored["result_recorded_at"]


@pytest.mark.asyncio
async def test_concurrent_callers_dispatch_only_once(context, monkeypatch):
    calls = []

    async def dispatch(host, envelope, source):
        calls.append(envelope)
        await asyncio.sleep(0.05)
        return result(envelope)

    async def observe(host, claim_id):
        raise transport.ValidationTransportError("active_host_slot")

    monkeypatch.setattr(transport, "dispatch", dispatch)
    monkeypatch.setattr(transport, "observe", observe)
    outcomes = await asyncio.gather(*[execute(context) for _ in range(6)])
    assert len(calls) == 1
    assert any(value["outcome"] == "passed" for value in outcomes)
    assert (await execute(context))["outcome"] == "passed"


@pytest.mark.asyncio
async def test_wrong_source_or_host_never_reserves(context):
    with pytest.raises(admission.ValidationConflict, match="source"):
        await execute(context, source=b"not-the-frozen-archive")
    with pytest.raises(admission.ValidationConflict, match="host"):
        await execute(context, host=SimpleNamespace(server_uuid="wrong", project_id="wrong"))
    async with context[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_validation_claims") == 0


@pytest.mark.asyncio
async def test_failed_test_is_not_failed_transport_and_is_not_repeated(context, monkeypatch):
    async def dispatch(host, envelope, source):
        return result(envelope, success=False, exit_code=1)

    monkeypatch.setattr(transport, "dispatch", dispatch)
    value = await execute(context)
    assert value["outcome"] == "failed" and value["result"]["exit_code"] == 1
    assert value["capacity_status"] == "released"


@pytest.mark.asyncio
async def test_lost_result_reconciles_cleanup_but_never_invents_pass(context, monkeypatch):
    issued = []

    async def dispatch(host, envelope, source):
        issued.append(envelope)
        raise transport.ValidationTransportError("host_transport_uncertain")

    async def observe(host, claim_id):
        envelope = issued[0]
        return {"claim_id": claim_id, "state": "cleaned", "cleanup_verified": True,
                "validation_job_id": "b" * 32, "validation_success": None,
                "request_hash": envelope["request_hash"],
                "binding_hash": hashlib.sha256(json.dumps(envelope, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()}

    monkeypatch.setattr(transport, "dispatch", dispatch)
    monkeypatch.setattr(transport, "observe", observe)
    first = await execute(context)
    assert first["outcome"] == "unknown" and first["capacity_status"] == "uncertain"
    second = await execute(context)
    assert second["outcome"] == "unknown" and second["capacity_status"] == "released"
    assert second["result"] is None and len(issued) == 1


@pytest.mark.asyncio
async def test_stale_worker_cannot_reserve_or_dispatch(context):
    with pytest.raises(admission.ValidationConflict, match="lease"):
        await execute(context, lease_owner="stale-worker")
    async with context[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_validation_claims") == 0


@pytest.mark.asyncio
async def test_lease_loss_between_reserve_and_dispatch_is_rechecked(context):
    pool, req, *_ = context
    row = await admission.reserve(pool, req, user_id="alice", idempotency_key="one",
                                  expected_lease_owner="worker-one")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE coding_runs SET lease_owner='worker-two'")
    with pytest.raises(admission.ValidationConflict, match="lease"):
        await admission.claim_dispatch(pool, row["id"], user_id="alice",
                                        expected_lease_owner="worker-one")
    issued = await admission.claim_dispatch(pool, row["id"], user_id="alice",
                                            expected_lease_owner="worker-two")
    assert issued["claim_id"] == row["id"]


@pytest.mark.asyncio
async def test_cancelled_controller_leaves_unknown_committed_intent(context, monkeypatch):
    async def dispatch(host, envelope, source):
        raise asyncio.CancelledError()

    monkeypatch.setattr(transport, "dispatch", dispatch)
    with pytest.raises(asyncio.CancelledError):
        await execute(context)
    async with context[0].acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM coding_validation_claims")
        assert row["status"] == "dispatched" and row["validation_outcome"] == "unknown"
        assert row["released_at"] is None


@pytest.mark.asyncio
async def test_result_transaction_failure_does_not_create_false_success(context, monkeypatch):
    async def dispatch(host, envelope, source):
        return result(envelope)

    original = admission._event

    async def event(conn, claim, kind):
        if kind == "result_recorded":
            raise RuntimeError("audit unavailable")
        return await original(conn, claim, kind)

    monkeypatch.setattr(transport, "dispatch", dispatch)
    monkeypatch.setattr(admission, "_event", event)
    with pytest.raises(RuntimeError, match="audit"):
        await execute(context)
    async with context[0].acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM coding_validation_claims")
        assert row["status"] == "dispatched" and row["validation_outcome"] == "unknown"
        assert row["encrypted_result"] is None and row["released_at"] is None


@pytest.mark.asyncio
async def test_expired_result_not_returned_and_private_payload_purged(context, monkeypatch):
    async def dispatch(host, envelope, source):
        return result(envelope)

    monkeypatch.setattr(transport, "dispatch", dispatch)
    value = await execute(context)
    async with context[0].acquire() as conn:
        await conn.execute("UPDATE coding_validation_claims SET result_recorded_at=now()-interval '2 days',"
                           "result_expires_at=now()-interval '1 day'")
    reread = await controller.recover(context[0], UUID(value["claim_id"]), user_id="alice",
                                      host=context[2])
    assert reread["outcome"] == "passed" and reread["result_available"] is False
    assert await controller.purge_expired_results(context[0]) == 1


@pytest.mark.asyncio
async def test_user_deletion_scrubs_result_but_preserves_cleanup_obligation(context, monkeypatch):
    async def dispatch(host, envelope, source):
        return result(envelope)

    monkeypatch.setattr(transport, "dispatch", dispatch)
    value = await execute(context)
    async with context[0].acquire() as conn:
        await conn.execute("DELETE FROM coding_runs WHERE id=$1", context[1].run_id)
        row = await conn.fetchrow("SELECT * FROM coding_validation_claims")
        assert row["run_id"] is None and row["encrypted_result"] is None
        assert row["host_job_id"] == "b" * 32
        await conn.execute("UPDATE coding_validation_claims SET encrypted_result='resurrection'")
        assert await conn.fetchval("SELECT encrypted_result FROM coding_validation_claims") is None
    with pytest.raises(admission.ValidationConflict, match="unavailable"):
        await controller.recover(context[0], UUID(value["claim_id"]), user_id="alice", host=context[2])


@pytest.mark.asyncio
async def test_unfinished_deleted_run_still_requires_identity_bound_cleanup(context):
    pool, req, *_ = context
    row = await admission.reserve(pool, req, user_id="alice", idempotency_key="one")
    issued = await admission.claim_dispatch(pool, row["id"], user_id="alice")
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM coding_runs WHERE id=$1", req.run_id)
        assert await conn.fetchval("SELECT released_at FROM coding_validation_claims") is None
    envelope = issued["host_envelope"]
    observation = {
        "claim_id": str(row["id"]), "state": "cleaned", "cleanup_verified": True,
        "validation_job_id": "b" * 32, "request_hash": envelope["request_hash"],
        "binding_hash": hashlib.sha256(json.dumps(envelope, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest(),
        "validation_success": None,
    }
    await admission.reconcile_host_observation(pool, row["id"], user_id="alice",
                                               observation=observation)
    async with pool.acquire() as conn:
        fresh = await conn.fetchrow("SELECT * FROM coding_validation_claims")
        assert fresh["status"] == "released" and fresh["validation_outcome"] == "unknown"
        assert fresh["encrypted_result"] is None


@pytest.mark.asyncio
async def test_result_migration_will_not_silently_discard_private_output(context, monkeypatch):
    import importlib
    import asyncpg

    async def dispatch(host, envelope, source):
        return result(envelope)

    monkeypatch.setattr(transport, "dispatch", dispatch)
    await execute(context)
    statements = []
    migration = importlib.import_module("migrations.versions.020_validation_results")
    monkeypatch.setattr(migration, "op", SimpleNamespace(execute=statements.append))
    migration.downgrade()
    async with context[0].acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="private validation results"):
            await conn.execute(statements[0])
        assert await conn.fetchval("SELECT encrypted_result FROM coding_validation_claims")
