import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from app.coding import cost_ledger, deployment_admission as deploy, host_capacity
from app.coding.cloud_budget import CloudCostRequest
from tests.coding.test_cost_ledger_postgres import DSN
from tests.coding.test_deployment_admission_postgres import prepared as prepared_fixture

pytestmark = pytest.mark.skipif(not DSN, reason="set COST_LEDGER_TEST_DSN to local PostgreSQL")
# Re-export a shared fixture without shadowing its name in function arguments.
prepared = prepared_fixture


@pytest_asyncio.fixture
async def covered(prepared):
    pool, target, manifest = prepared
    reservation = await cost_ledger.reserve(
        pool, CloudCostRequest(project_id=target.project_id, category="hosting",
                               estimated_upper_bound_paise=1_400_000, planned_end_at=manifest.planned_end_at),
        user_id="host-operator", idempotency_key="funded-host",
    )
    await cost_ledger.start_dispatch(pool, project_id=target.project_id,
                                     reservation_id=reservation["id"], user_id="host-operator")
    host = host_capacity.FundedHost(
        pool_id="funded-delhi-host", project_id=target.project_id, server_uuid=target.server_uuid,
        funding_reservation_id=reservation["id"], funding_owner_user_id="host-operator",
        cpu_millis=2000, memory_mib=8192, storage_mib=20480, enforcement_hash="f" * 64,
    )
    await host_capacity.register_pool(pool, host, evidence_reference="test:verified-runtime-limits")
    target = target.model_copy(update={"target_id": "covered-target", "capacity_pool_id": host.pool_id})
    await deploy.register_target(pool, target, evidence_reference="test:verified-covered-target")
    manifest = manifest.model_copy(update={
        "target_id": target.target_id, "target_hash": deploy.fingerprint(target),
        "estimated_upper_bound_paise": 0,
        "capacity": host_capacity.CapacityRequest(pool_id=host.pool_id, cpu_millis=1000,
                                                  memory_mib=2048, storage_mib=4096),
    })
    return pool, target, manifest, reservation, host


async def queue(covered, **changes):
    pool, _, manifest, _, _ = covered
    manifest = manifest.model_copy(update={"request_id": uuid4(), **changes})
    await deploy.record_user_grant(pool, manifest, user_id="alice", authority_reference="test:user-request",
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    return await deploy.enqueue(pool, manifest, user_id="alice", idempotency_key=str(manifest.request_id))


@pytest.mark.asyncio
async def test_two_apps_share_one_host_cost_without_double_charge(covered):
    rows = await asyncio.gather(*[queue(covered) for _ in range(6)], return_exceptions=True)
    accepted = [row for row in rows if isinstance(row, dict)]
    assert len(accepted) == 2
    assert sum(isinstance(row, cost_ledger.CostAdmissionDenied) for row in rows) == 4
    for row in accepted:
        assert row["reservation_id"] is None
        assert await deploy.claim_dispatch(covered[0], row["id"], user_id="alice",
                                           require_validation=False)
        assert await deploy.claim_dispatch(covered[0], row["id"], user_id="alice",
                                           require_validation=False) is None
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 1
        assert await conn.fetchval("SELECT estimated_paise FROM cloud_cost_reservations") == 1_400_000


@pytest.mark.asyncio
@pytest.mark.parametrize("field,amount", [("cpu_millis", 2001), ("memory_mib", 8193), ("storage_mib", 20481)])
async def test_independent_resource_limits(covered, field, amount):
    cap = covered[2].capacity.model_copy(update={field: amount})
    with pytest.raises(cost_ledger.CostAdmissionDenied, match=field):
        await queue(covered, capacity=cap)


@pytest.mark.asyncio
async def test_unknown_or_mismatched_pool_cannot_make_cost_free(covered):
    wrong = covered[2].capacity.model_copy(update={"pool_id": "some-other-pool"})
    with pytest.raises(deploy.DeploymentConflict, match="coverage"):
        await queue(covered, capacity=wrong)
    with pytest.raises(ValueError, match="coverage"):
        deploy.DeploymentManifest.model_validate({**covered[2].model_dump(), "capacity": None})


@pytest.mark.asyncio
async def test_unknown_provider_outcome_keeps_capacity_until_verified_release(covered):
    cap = covered[2].capacity.model_copy(update={"cpu_millis": 2000})
    row = await queue(covered, capacity=cap)
    await deploy.claim_dispatch(covered[0], row["id"], user_id="alice",
                                require_validation=False)
    await deploy.record_provider_observation(covered[0], row["id"], user_id="alice", evidence_hash="a" * 64)
    with pytest.raises(cost_ledger.CostAdmissionDenied, match="capacity_exceeded"):
        await queue(covered)
    released = await deploy.release_capacity(covered[0], row["id"], user_id="alice", evidence_hash="b" * 64)
    assert released["status"] == "retired"
    assert await deploy.claim_dispatch(covered[0], row["id"], user_id="alice",
                                       require_validation=False) is None
    assert (await queue(covered))["status"] == "reserved"
    async with covered[0].acquire() as conn:
        assert await conn.fetchval("SELECT status FROM cloud_cost_reservations") == "dispatched"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "funding_uncertain", "funding_overrun", "expired"])
async def test_coverage_rechecked_at_dispatch(covered, change):
    row = await queue(covered)
    statements = {
        "disabled": "UPDATE coding_host_capacity SET enabled=false",
        "funding_uncertain": "UPDATE cloud_cost_reservations SET status='uncertain'",
        "funding_overrun": "UPDATE cloud_cost_reservations SET accrued_paise=1500000",
        "expired": "UPDATE cloud_cost_reservations SET planned_end_at=now()-interval '1 second'",
    }
    async with covered[0].acquire() as conn:
        await conn.execute(statements[change])
    with pytest.raises(cost_ledger.CostAdmissionDenied):
        await deploy.claim_dispatch(covered[0], row["id"], user_id="alice",
                                    require_validation=False)


@pytest.mark.asyncio
async def test_host_limit_increase_and_duplicate_alias_rejected(covered):
    pool, _, _, _, host = covered
    for change in ({"cpu_millis": 4000}, {"pool_id": "same-host-new-pool"}):
        with pytest.raises(cost_ledger.CostLedgerConflict):
            await host_capacity.register_pool(pool, host.model_copy(update=change), evidence_reference="test:invalid")
