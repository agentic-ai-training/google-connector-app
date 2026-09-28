"""Real local PostgreSQL proofs; no Coolify write or cloud deployment is performed."""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
import asyncpg

from app.coding import deployment_admission as deploy
from app.coding import cost_ledger
from app.coding.cost_ledger import CostAdmissionDenied
from tests.coding.test_cost_ledger_postgres import DSN, PROJECT

pytestmark = pytest.mark.skipif(not DSN, reason="set COST_LEDGER_TEST_DSN to local PostgreSQL")


@pytest_asyncio.fixture
async def prepared(db):
    pool, _, end = db
    async with pool.acquire() as conn:
        schema = importlib.import_module("migrations.versions.017_deployment_admission")
        await conn.execute(schema.SCHEMA_SQL)
        capacity_schema = importlib.import_module("migrations.versions.018_host_capacity")
        await conn.execute(capacity_schema.SCHEMA_SQL)
        # Minimal real relational fixture; the complete production schema is checked
        # separately by verify_cloud_migrations_local.py's full Alembic round trip.
        await conn.execute("""CREATE TABLE coding_runs(
            id UUID PRIMARY KEY,user_id TEXT NOT NULL,status TEXT NOT NULL,
            source_egress_consented_at TIMESTAMPTZ,deleted_at TIMESTAMPTZ)""")
        validation_schema = importlib.import_module("migrations.versions.019_validation_capacity")
        await conn.execute(validation_schema.SCHEMA_SQL)
        results_schema = importlib.import_module("migrations.versions.020_validation_results")
        await conn.execute(results_schema.SCHEMA_SQL)
    target = deploy.RegisteredTarget(
        target_id="test-target", base_url="http://127.0.0.1:18000", team_id=0,
        inventory_secret_ref="test/inventory", configuration_secret_ref="test/configuration",
        deployment_secret_ref="test/deploy", project_id=PROJECT, server_uuid="server",
        coolify_project_uuid="project", environment_uuid="environment", owner_user_id="alice",
    )
    await deploy.register_target(pool, target, evidence_reference="test:operator-verified")
    manifest = deploy.DeploymentManifest(
        request_id=uuid4(), target_id=target.target_id, target_hash=deploy.fingerprint(target),
        release_hash="a" * 64, compose_hash="b" * 64, validation_attestation_hash="c" * 64,
        estimated_upper_bound_paise=100_000, planned_end_at=end,
    )
    return pool, target, manifest


async def authorize(prepared):
    pool, _, manifest = prepared
    return await deploy.record_user_grant(
        pool, manifest, user_id="alice", authority_reference="test:authenticated-user-approval",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def enqueue(prepared, **kwargs):
    return await deploy.enqueue(prepared[0], prepared[2], user_id="alice",
                                idempotency_key=kwargs.pop("idempotency_key", "request-one"), **kwargs)


@pytest.mark.asyncio
async def test_no_grant_means_no_deployment_or_reserved_money(prepared):
    with pytest.raises(deploy.DeploymentConflict, match="authorization"):
        await enqueue(prepared)
    async with prepared[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 0
        assert await conn.fetchval("SELECT count(*) FROM coding_deployments") == 0


@pytest.mark.asyncio
async def test_target_cannot_be_overwritten_or_transferred(prepared):
    pool, target, _ = prepared
    for change in ({"base_url": "https://other.example"}, {"owner_user_id": "mallory"},
                   {"server_uuid": "other-server"}):
        with pytest.raises(deploy.DeploymentConflict, match="immutable"):
            await deploy.register_target(pool, target.model_copy(update=change), evidence_reference="changed")


@pytest.mark.asyncio
async def test_duplicate_enqueue_and_dispatch_have_one_cost_and_one_authority(prepared):
    await authorize(prepared)
    rows = await asyncio.gather(*[enqueue(prepared) for _ in range(8)])
    assert len({row["id"] for row in rows}) == 1
    claims = await asyncio.gather(*[
        deploy.claim_dispatch(prepared[0], rows[0]["id"], user_id="alice") for _ in range(8)
    ])
    assert sum(claim is not None for claim in claims) == 1
    async with prepared[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 1
        assert await conn.fetchval("SELECT status FROM cloud_cost_reservations") == "dispatched"
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "dispatching"


@pytest.mark.asyncio
async def test_one_grant_cannot_fund_two_dispatches(prepared):
    await authorize(prepared)
    await enqueue(prepared)
    with pytest.raises(deploy.DeploymentConflict, match="already bound"):
        await enqueue(prepared, idempotency_key="different-key")


@pytest.mark.asyncio
async def test_different_release_cannot_reuse_approval_or_idempotency(prepared):
    await authorize(prepared)
    await enqueue(prepared)
    pool, _, manifest = prepared
    changed = manifest.model_copy(update={"release_hash": "d" * 64})
    with pytest.raises(deploy.DeploymentConflict, match="idempotency"):
        await deploy.enqueue(pool, changed, user_id="alice", idempotency_key="request-one")
    with pytest.raises(deploy.DeploymentConflict, match="authorization"):
        await deploy.enqueue(pool, changed, user_id="alice", idempotency_key="request-two")


@pytest.mark.asyncio
async def test_wrong_tenant_cannot_approve_enqueue_or_claim(prepared):
    pool, _, manifest = prepared
    with pytest.raises(deploy.DeploymentConflict):
        await deploy.record_user_grant(pool, manifest, user_id="mallory", authority_reference="fake",
                                       expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    with pytest.raises(deploy.DeploymentConflict):
        await deploy.enqueue(pool, manifest, user_id="mallory", idempotency_key="request-one")
    await authorize(prepared)
    row = await enqueue(prepared)
    with pytest.raises(deploy.DeploymentConflict):
        await deploy.claim_dispatch(pool, row["id"], user_id="mallory")


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["grant", "target", "expiry", "stale_credit"])
async def test_revocation_or_expiry_rechecked_at_dispatch(prepared, revocation):
    grant = await authorize(prepared)
    row = await enqueue(prepared)
    commands = {
        "expiry": "UPDATE coding_deployment_grants SET expires_at=now()-interval '1 second'",
        "stale_credit": "UPDATE cloud_budget_accounts SET observed_at=now()-interval '2 hours'",
    }
    if revocation == "grant":
        await deploy.revoke_grant(prepared[0], grant["id"], user_id="alice")
    elif revocation == "target":
        await deploy.revoke_target(prepared[0], prepared[1].target_id, user_id="alice")
    else:
        async with prepared[0].acquire() as conn:
            await conn.execute(commands[revocation])
    with pytest.raises((deploy.DeploymentConflict, CostAdmissionDenied)):
        await deploy.claim_dispatch(prepared[0], row["id"], user_id="alice")
    async with prepared[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM cloud_cost_reservations") == "reserved"
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "reserved"


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_cost_and_deployment(prepared, monkeypatch):
    await authorize(prepared)

    async def fail(*args):
        raise RuntimeError("simulated deployment audit failure")

    monkeypatch.setattr(deploy, "_event", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        await enqueue(prepared)
    async with prepared[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 0
        assert await conn.fetchval("SELECT count(*) FROM coding_deployments") == 0


@pytest.mark.asyncio
async def test_dispatch_audit_failure_rolls_back_both_intents(prepared, monkeypatch):
    await authorize(prepared)
    row = await enqueue(prepared)

    async def fail(*args):
        raise RuntimeError("simulated commit boundary failure")

    monkeypatch.setattr(deploy, "_event", fail)
    with pytest.raises(RuntimeError, match="commit boundary failure"):
        await deploy.claim_dispatch(prepared[0], row["id"], user_id="alice")
    async with prepared[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM cloud_cost_reservations") == "reserved"
        assert await conn.fetchval("SELECT status FROM coding_deployments") == "reserved"


@pytest.mark.asyncio
async def test_paid_candidate_cannot_enter_user_service_lane(prepared):
    with pytest.raises(ValueError):
        deploy.DeploymentManifest.model_validate({**prepared[2].model_dump(), "origin": "core_candidate"})


@pytest.mark.asyncio
async def test_database_constraints_prevent_ownership_rebinding(prepared):
    await authorize(prepared)
    await enqueue(prepared)
    async with prepared[0].acquire() as conn:
        for table in ("cloud_cost_reservations", "coding_deployment_grants", "coding_deployments"):
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(f"UPDATE {table} SET user_id='mallory'")


@pytest.mark.asyncio
async def test_uncertain_write_never_replays_and_can_reconcile_after_revocation(prepared):
    await authorize(prepared)
    row = await enqueue(prepared)
    await deploy.claim_dispatch(prepared[0], row["id"], user_id="alice")
    uncertain = await deploy.record_provider_observation(
        prepared[0], row["id"], user_id="alice", evidence_hash="d" * 64,
    )
    assert uncertain["status"] == "uncertain"
    assert await deploy.claim_dispatch(prepared[0], row["id"], user_id="alice") is None
    async with prepared[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM cloud_cost_reservations") == "uncertain"
    await deploy.revoke_target(prepared[0], prepared[1].target_id, user_id="alice")
    submitted = await deploy.record_provider_observation(
        prepared[0], row["id"], user_id="alice", evidence_hash="e" * 64, resource_id="actual-resource",
    )
    assert submitted["status"] == "submitted"  # deliberately NOT verified/completed
    assert (await deploy.record_provider_observation(
        prepared[0], row["id"], user_id="alice", evidence_hash="e" * 64, resource_id="actual-resource",
    ))["id"] == row["id"]
    with pytest.raises(deploy.DeploymentConflict, match="cannot be replaced"):
        await deploy.record_provider_observation(
            prepared[0], row["id"], user_id="alice", evidence_hash="e" * 64, resource_id="another-resource",
        )


@pytest.mark.asyncio
async def test_provider_claim_before_dispatch_is_rejected(prepared):
    await authorize(prepared)
    row = await enqueue(prepared)
    with pytest.raises(deploy.DeploymentConflict, match="no dispatch"):
        await deploy.record_provider_observation(
            prepared[0], row["id"], user_id="alice", evidence_hash="e" * 64, resource_id="fake-resource",
        )


@pytest.mark.asyncio
async def test_migration_017_round_trip_preserves_cost_account(prepared, monkeypatch):
    statements = []
    migration = importlib.import_module("migrations.versions.017_deployment_admission")

    class Capture:
        @staticmethod
        def execute(sql):
            statements.append(sql)

    monkeypatch.setattr(migration, "op", Capture)
    capacity_migration = importlib.import_module("migrations.versions.018_host_capacity")
    monkeypatch.setattr(capacity_migration, "op", Capture)
    validation_migration = importlib.import_module("migrations.versions.019_validation_capacity")
    monkeypatch.setattr(validation_migration, "op", Capture)
    result_migration = importlib.import_module("migrations.versions.020_validation_results")
    monkeypatch.setattr(result_migration, "op", Capture)
    result_migration.downgrade()
    validation_migration.downgrade()
    capacity_migration.downgrade()
    migration.downgrade()
    async with prepared[0].acquire() as conn:
        for statement in statements:
            await conn.execute(statement)
        assert await conn.fetchval("SELECT count(*) FROM cloud_budget_accounts") == 1
        await conn.execute(migration.SCHEMA_SQL)
        await conn.execute(capacity_migration.SCHEMA_SQL)
        await conn.execute(validation_migration.SCHEMA_SQL)
        await conn.execute(result_migration.SCHEMA_SQL)
        assert await conn.fetchval("SELECT count(*) FROM coding_deployments") == 0


@pytest.mark.asyncio
async def test_idempotency_namespace_is_atomic_across_funding_projects(prepared, db):
    pool, target, manifest = prepared
    second_snapshot = db[1].model_copy(update={"project_id": "other-test-project"})
    await cost_ledger.register_account(pool, second_snapshot, hosting_ends_at=db[2],
                                      evidence_reference="test:second-account")
    second_target = target.model_copy(update={"target_id": "second-target", "project_id": "other-test-project"})
    await deploy.register_target(pool, second_target, evidence_reference="test:second-target")
    second_manifest = manifest.model_copy(update={
        "target_id": second_target.target_id, "target_hash": deploy.fingerprint(second_target),
        "request_id": uuid4(),
    })
    await authorize(prepared)
    await authorize((pool, second_target, second_manifest))
    results = await asyncio.gather(*[
        deploy.enqueue(pool, current, user_id="alice", idempotency_key="same-key")
        for current in (manifest, second_manifest)
    ], return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, deploy.DeploymentConflict) for result in results) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_deployments") == 1
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 1
