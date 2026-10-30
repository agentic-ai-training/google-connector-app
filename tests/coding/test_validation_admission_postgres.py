import asyncio
import importlib
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.coding import cost_ledger, deployment_admission as deployment
from app.coding import validation_admission as validation
from tests.coding.test_deployment_admission_postgres import prepared as prepared_fixture
from tests.coding.test_host_capacity_postgres import covered as covered_fixture, queue


prepared = prepared_fixture
covered = covered_fixture


async def request(covered, **changes):
    pool, target, _, _, _ = covered
    run_id = uuid4()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO coding_runs VALUES($1,'alice','planning',now(),NULL)", run_id)
    return validation.ValidationRequest(**{
        "run_id": run_id, "sequence_no": 0, "target_id": target.target_id,
        "target_hash": deployment.fingerprint(target), "source_hash": "a" * 64,
        "image_id": "sha256:" + "b" * 64, "profile": "python_compile", "timeout_seconds": 20,
        "planned_end_at": datetime.now(timezone.utc) + timedelta(minutes=10), **changes,
    })


async def reserve(covered, req=None, key=None):
    req = req or await request(covered)
    return await validation.reserve(covered[0], req, user_id="alice", idempotency_key=key or str(req.run_id))


@pytest.mark.asyncio
async def test_validation_and_deployments_share_the_same_capacity(covered):
    requests = [await request(covered) for _ in range(4)]
    results = await asyncio.gather(
        *[reserve(covered, req) for req in requests], queue(covered), queue(covered),
        return_exceptions=True,
    )
    assert sum(isinstance(row, dict) for row in results) == 2
    assert sum(isinstance(row, cost_ledger.CostAdmissionDenied) for row in results) == 4
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("""SELECT sum(cpu) FROM (
          SELECT sum(cpu_millis) AS cpu FROM coding_deployments WHERE capacity_released_at IS NULL
          UNION ALL SELECT sum(cpu_millis) FROM coding_validation_claims WHERE released_at IS NULL
        ) t""") == 2000
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 1


@pytest.mark.asyncio
async def test_replay_has_one_claim_and_one_dispatch(covered):
    req = await request(covered)
    rows = await asyncio.gather(*[reserve(covered, req) for _ in range(8)])
    assert len({row["id"] for row in rows}) == 1
    claims = await asyncio.gather(*[
        validation.claim_dispatch(covered[0], rows[0]["id"], user_id="alice") for _ in range(8)
    ])
    assert sum(claim is not None for claim in claims) == 1
    issued = next(claim for claim in claims if claim is not None)["host_envelope"]
    assert issued["claim_id"] == str(rows[0]["id"])
    assert issued["run_id"] == str(req.run_id)
    assert issued["source_hash"] == req.source_hash
    assert issued["request_hash"] == deployment.fingerprint(req)
    assert issued["server_uuid"] == covered[1].server_uuid
    assert issued["deadline"] == int(req.planned_end_at.timestamp())


@pytest.mark.asyncio
async def test_bound_host_reconciliation_rejects_wrong_claim_or_binding(covered):
    row = await reserve(covered)
    issued = await validation.claim_dispatch(covered[0], row["id"], user_id="alice")
    binding_hash = hashlib.sha256(json.dumps(
        issued["host_envelope"], sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    observed = {"claim_id": str(row["id"]), "state": "cleaned", "cleanup_verified": True,
                "validation_job_id": "e" * 32, "request_hash": row["request_hash"],
                "binding_hash": binding_hash, "validation_success": None}
    for field, value in [("claim_id", str(uuid4())), ("binding_hash", "a" * 64),
                         ("request_hash", "b" * 64), ("validation_success", True),
                         ("cleanup_verified", "true"), ("state", "running")]:
        with pytest.raises(validation.ValidationConflict):
            await validation.reconcile_host_observation(
                covered[0], row["id"], user_id="alice", observation={**observed, field: value},
            )
    result = await validation.reconcile_host_observation(
        covered[0], row["id"], user_id="alice", observation=observed,
    )
    assert result["status"] == "released"
    assert result["host_job_id"] == "e" * 32


@pytest.mark.asyncio
async def test_unknown_host_claim_retains_central_allocation(covered):
    row = await reserve(covered)
    await validation.claim_dispatch(covered[0], row["id"], user_id="alice")
    observation = {"claim_id": str(row["id"]), "state": "unknown", "cleanup_verified": False}
    result = await validation.reconcile_host_observation(
        covered[0], row["id"], user_id="alice", observation=observation,
    )
    assert result["status"] == "uncertain"
    assert result["released_at"] is None
    with pytest.raises(validation.ValidationConflict):
        await validation.reconcile_host_observation(
            covered[0], row["id"], user_id="alice",
            observation={**observation, "cleanup_verified": True},
        )


@pytest.mark.asyncio
async def test_unknown_outcome_retains_capacity_and_known_host_identity(covered):
    row = await reserve(covered)
    await queue(covered)
    await validation.claim_dispatch(covered[0], row["id"], user_id="alice")
    await validation.observe(covered[0], row["id"], user_id="alice", evidence_hash="c" * 64,
                             host_job_id="d" * 32)
    await validation.observe(covered[0], row["id"], user_id="alice", evidence_hash="e" * 64)
    with pytest.raises(cost_ledger.CostAdmissionDenied):
        await reserve(covered)
    with pytest.raises(validation.ValidationConflict, match="identity"):
        await validation.observe(covered[0], row["id"], user_id="alice", evidence_hash="f" * 64,
                                 host_job_id="a" * 32, cleanup_verified=True)
    await validation.observe(covered[0], row["id"], user_id="alice", evidence_hash="f" * 64,
                             host_job_id="d" * 32, cleanup_verified=True)
    assert await reserve(covered)
    assert await validation.claim_dispatch(covered[0], row["id"], user_id="alice") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancelled", "deleted", "no_consent", "funding", "target"])
async def test_authority_rechecked_before_dispatch(covered, change):
    row = await reserve(covered)
    statements = {
        "cancelled": "UPDATE coding_runs SET status='cancelled'",
        "deleted": "UPDATE coding_runs SET deleted_at=now()",
        "no_consent": "UPDATE coding_runs SET source_egress_consented_at=NULL",
        "funding": "UPDATE cloud_cost_reservations SET status='uncertain'",
        "target": "UPDATE coding_deployment_targets SET status='revoked'",
    }
    async with covered[0].acquire() as conn:
        await conn.execute(statements[change])
    with pytest.raises((validation.ValidationConflict, deployment.DeploymentConflict,
                        cost_ledger.CostAdmissionDenied)):
        await validation.claim_dispatch(covered[0], row["id"], user_id="alice")
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT released_at FROM coding_validation_claims") is None


@pytest.mark.asyncio
async def test_deadline_does_not_automatically_release_a_dispatched_claim(covered):
    row = await reserve(covered)
    await validation.claim_dispatch(covered[0], row["id"], user_id="alice")
    async with covered[0].acquire() as conn:
        await conn.execute("UPDATE coding_validation_claims SET planned_end_at=now()-interval '1 hour'")
    with pytest.raises(validation.ValidationConflict, match="reconciliation"):
        await validation.cancel_before_dispatch(covered[0], row["id"], user_id="alice")


@pytest.mark.asyncio
async def test_cross_tenant_access_and_foreign_run_binding_rejected(covered):
    req = await request(covered)
    row = await reserve(covered, req)
    with pytest.raises(validation.ValidationConflict, match="unavailable"):
        await validation.claim_dispatch(covered[0], row["id"], user_id="mallory")
    async with covered[0].acquire() as conn:
        await conn.execute("INSERT INTO coding_runs VALUES($1,'mallory','planning',now(),NULL)",
                           foreign := uuid4())
    with pytest.raises(validation.ValidationConflict, match="this user"):
        await reserve(covered, req.model_copy(update={"run_id": foreign}))


@pytest.mark.asyncio
async def test_changed_source_cannot_reuse_idempotency_and_cancel_is_safe(covered):
    req = await request(covered)
    row = await reserve(covered, req, "same")
    with pytest.raises(validation.ValidationConflict, match="idempotency"):
        await reserve(covered, req.model_copy(update={"source_hash": "f" * 64}), "same")
    await validation.cancel_before_dispatch(covered[0], row["id"], user_id="alice")
    await validation.cancel_before_dispatch(covered[0], row["id"], user_id="alice")
    assert await validation.claim_dispatch(covered[0], row["id"], user_id="alice") is None


@pytest.mark.asyncio
async def test_one_host_cleanup_identity_cannot_release_two_claims(covered):
    first, second = await reserve(covered), await reserve(covered)
    for row in (first, second):
        await validation.claim_dispatch(covered[0], row["id"], user_id="alice")
    await validation.observe(covered[0], first["id"], user_id="alice", evidence_hash="f" * 64,
                             host_job_id="a" * 32, cleanup_verified=True)
    import asyncpg
    with pytest.raises(asyncpg.UniqueViolationError):
        await validation.observe(covered[0], second["id"], user_id="alice", evidence_hash="f" * 64,
                                 host_job_id="a" * 32, cleanup_verified=True)
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT released_at FROM coding_validation_claims WHERE id=$1",
                                   second["id"]) is None


@pytest.mark.asyncio
async def test_migration_refuses_to_erase_unreconciled_claims(covered, monkeypatch):
    row = await reserve(covered)
    statements = []
    migration = importlib.import_module("migrations.versions.019_validation_capacity")

    class Capture:
        @staticmethod
        def execute(sql):
            statements.append(sql)

    monkeypatch.setattr(migration, "op", Capture)
    migration.downgrade()
    import asyncpg
    async with covered[0].acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="reconcile"):
            await conn.execute(statements[0])
        assert await conn.fetchval("SELECT id FROM coding_validation_claims") == row["id"]
    await validation.cancel_before_dispatch(covered[0], row["id"], user_id="alice")
    image_migration = importlib.import_module("migrations.versions.021_image_build_jobs")
    monkeypatch.setattr(image_migration, "op", Capture)
    image_migration.downgrade()
    async with covered[0].acquire() as conn:
        await conn.execute(statements[1])
        await conn.execute(statements[0])
        await conn.execute(migration.SCHEMA_SQL)
        await conn.execute(image_migration.SCHEMA_SQL)
        assert await conn.fetchval("SELECT count(*) FROM coding_validation_claims") == 0


@pytest.mark.asyncio
async def test_event_failure_rolls_back_capacity_claim(covered, monkeypatch):
    async def unavailable(*args):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(validation, "_event", unavailable)
    with pytest.raises(RuntimeError, match="audit"):
        await reserve(covered)
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_validation_claims") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 30, 3600])
async def test_short_lived_authority_cannot_outlive_or_underfund_execution(covered, offset):
    req = await request(covered, planned_end_at=datetime.now(timezone.utc) + timedelta(seconds=offset))
    with pytest.raises(validation.ValidationConflict):
        await reserve(covered, req)


@pytest.mark.asyncio
async def test_string_cleanup_claim_is_not_boolean_authority(covered):
    row = await reserve(covered)
    with pytest.raises(ValueError, match="boolean"):
        await validation.observe(covered[0], row["id"], user_id="alice", evidence_hash="e" * 64,
                                 host_job_id="a" * 32, cleanup_verified="false")


@pytest.mark.asyncio
async def test_changed_relational_step_binding_cannot_dispatch(covered):
    row = await reserve(covered)
    async with covered[0].acquire() as conn:
        await conn.execute("UPDATE coding_validation_claims SET sequence_no=99 WHERE id=$1", row["id"])
    with pytest.raises(validation.ValidationConflict, match="binding changed"):
        await validation.claim_dispatch(covered[0], row["id"], user_id="alice")
