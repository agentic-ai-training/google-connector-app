"""Audited import proof; disposable loopback PostgreSQL only."""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
import os
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.coding import cost_ledger as ledger
from app.coding import deployment_admission as deploy, host_capacity
from app.coding.gce_identity import GCEIdentityError, VerifiedExistingVM
from app.coding.cloud_budget import CloudCostRequest


class HistoricalDateTime(datetime):
    """Explicit simulation of the original approved term, never provider evidence."""
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 13, 11, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def historical_business_clock(monkeypatch):
    # Application and disposable-schema SQL clocks agree. Do not change the
    # PostgreSQL server clock, production deadlines or any non-test schema.
    monkeypatch.setattr(sys.modules[__name__], 'datetime', HistoricalDateTime)
    for module in (ledger, deploy, host_capacity):
        monkeypatch.setattr(module, 'datetime', HistoricalDateTime)


@pytest_asyncio.fixture
async def virgin():
    dsn = os.getenv("COST_LEDGER_TEST_DSN")
    if not dsn:
        pytest.skip("set COST_LEDGER_TEST_DSN to local PostgreSQL")
    parsed = urlsplit(dsn)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.query:
        pytest.fail("VM import tests refuse non-loopback databases")
    schema = "cost_ledger_test_" + uuid4().hex
    conn = await asyncpg.connect(dsn)
    pool = None
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        migration = importlib.import_module("migrations.versions.016_cloud_cost_ledger")
        await conn.execute(f'SET search_path TO "{schema}",pg_catalog')
        await conn.execute(f'''CREATE FUNCTION "{schema}".now() RETURNS timestamptz
            LANGUAGE SQL STABLE AS $$ SELECT TIMESTAMPTZ '2026-10-13 11:00:00+00' $$''')
        await conn.execute(migration.SCHEMA_SQL)
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10,
                                         server_settings={"search_path": schema + ',pg_catalog'})
        assert await pool.fetchval('SELECT now()') == HistoricalDateTime.now(timezone.utc)
        yield pool
    finally:
        if pool:
            await pool.close()
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


def evidence(*, remaining=2_769_388, accrued=100_000, estimate=1_385_056):
    now = datetime.now(timezone.utc)
    return ledger.ExistingDelhiVM(
        project_id="project-ee0c1944-144c-4364-ad4", instance_id="822115653245959101",
        instance_name="gca-coolify-delhi-01", zone="asia-south2-a",
        machine_type="e2-standard-4", disk_type="pd-balanced", disk_gib=100,
        status="RUNNING", created_at=datetime(2026, 9, 19, 17, 24, 34, 872000, timezone.utc),
        observed_at=now, stop_at=datetime(2026, 10, 19, 17, 24, 32, tzinfo=timezone.utc),
        initial_trial_credit_paise=2_869_388, remaining_trial_credit_paise=remaining,
        credit_observed_at=now,
        credit_admission_cutoff=datetime(2026, 11, 20, tzinfo=timezone.utc),
        accrued_hosting_paise=accrued, charge_observed_at=now,
        estimated_total_hosting_paise=estimate,
        identity_evidence_reference="test:gce-instance-identity",
        credit_evidence_reference="test:fresh-promo-credit",
        charge_evidence_reference="test:attributable-compute-and-disk-charge",
    )


def test_exact_trial_total_is_required_not_earlier_rounded_figure():
    item = evidence()
    assert item.initial_trial_credit_paise == 2_869_388
    with pytest.raises(ValidationError):
        ledger.ExistingDelhiVM.model_validate({
            **item.model_dump(), "initial_trial_credit_paise": 2_869_400,
        })
    with pytest.raises(ValidationError):
        ledger.ExistingDelhiVM.model_validate({
            **item.model_dump(), "remaining_trial_credit_paise": 2_869_389,
        })


def test_fresh_credit_does_not_extend_an_expired_vm_term(monkeypatch):
    now = HistoricalDateTime(2026, 10, 22, 6, tzinfo=timezone.utc)
    item = evidence().model_copy(update={'observed_at': now, 'credit_observed_at': now, 'charge_observed_at': now})
    monkeypatch.setattr(ledger, '_now', lambda: now)
    with pytest.raises(ledger.CostAdmissionDenied, match='current evidence'):
        ledger._import_preflight(item)


@pytest.fixture(autouse=True)
def verified_provider_identity(monkeypatch):
    async def read(_host):
        item = evidence()
        return VerifiedExistingVM(**{
            key: getattr(item, key) for key in (
                "project_id", "instance_id", "instance_name", "zone", "machine_type",
                "disk_type", "disk_gib", "status", "created_at", "stop_at",
            )
        })

    monkeypatch.setattr(ledger.gce_identity, "read_existing_delhi_vm", read)


async def imported(pool, item=None, user="operator"):
    return await ledger.import_existing_delhi_vm(
        pool, item or evidence(), owner_user_id=user, operator_host=object(),
    )


@pytest.mark.asyncio
async def test_existing_liability_is_imported_once_without_provider_write(virgin):
    item = evidence()
    rows = await asyncio.gather(*[imported(virgin, item) for _ in range(8)])
    assert len({row["id"] for row in rows}) == 1
    row = rows[0]
    assert row["status"] == "dispatched"
    assert row["accrued_paise"] == 100_000
    assert row["estimated_paise"] == 1_385_056
    assert row["planned_end_at"] == item.stop_at
    async with virgin.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_budget_accounts") == 1
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 1
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_events") == 2
    # Reserve/dispatch is specifically for a NEW provider write; import never grants it.
    assert await ledger.start_dispatch(
        virgin, project_id=item.project_id, reservation_id=row["id"], user_id="operator",
    ) is False


@pytest.mark.asyncio
async def test_import_protects_vertex_and_future_reserve(virgin):
    item = evidence()
    await imported(virgin, item)
    vertex = CloudCostRequest(project_id=item.project_id, category="vertex",
                              estimated_upper_bound_paise=850_000,
                              planned_end_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert (await ledger.reserve(virgin, vertex, user_id="operator",
                                 idempotency_key="vertex"))["status"] == "reserved"
    with pytest.raises(ledger.CostAdmissionDenied):
        await ledger.reserve(virgin, vertex.model_copy(update={
            "estimated_upper_bound_paise": 1,
        }), user_id="operator", idempotency_key="more-vertex")


@pytest.mark.asyncio
async def test_import_underfunded_truthfully_and_denies_new_host_capacity(virgin):
    item = evidence(remaining=1_000_000)
    row = await imported(virgin, item)
    assert row["status"] == "uncertain"
    async with virgin.acquire() as conn:
        assert await conn.fetchval("SELECT event_type FROM cloud_cost_events "
                                   "ORDER BY id DESC LIMIT 1") == "existing_vm_imported_underfunded"


@pytest.mark.asyncio
async def test_stale_or_mismatched_identity_cannot_first_enroll(virgin):
    item = evidence()
    for changes in ({"credit_observed_at": datetime.now(timezone.utc)-timedelta(hours=2)},
                    {"stop_at": item.stop_at+timedelta(seconds=1)},
                    {"created_at": item.created_at+timedelta(seconds=1)},
                    {"estimated_total_hosting_paise": 1_400_001}):
        with pytest.raises(ledger.CostAdmissionDenied):
            await imported(virgin, item.model_copy(update=changes))
    async with virgin.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_budget_accounts") == 0


@pytest.mark.asyncio
async def test_import_cannot_replace_owner_cost_identity_or_account(virgin):
    item = evidence()
    await imported(virgin, item)
    for changed, user in ((item.model_copy(update={"accrued_hosting_paise": 100_001}), "operator"),
                          (item, "different-user")):
        with pytest.raises(ledger.CostLedgerConflict, match="cannot reset"):
            await imported(virgin, changed, user=user)


@pytest.mark.asyncio
async def test_import_audit_failure_rolls_back_account_and_obligation(virgin, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(ledger, "_event", fail)
    with pytest.raises(RuntimeError, match="audit"):
        await imported(virgin)
    async with virgin.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_budget_accounts") == 0
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 0


@pytest.mark.asyncio
async def test_live_provider_disagreement_or_unavailability_never_enrolls(virgin, monkeypatch):
    async def mismatch(_host):
        item = evidence()
        return VerifiedExistingVM(**{
            **{key: getattr(item, key) for key in (
                "project_id", "instance_id", "instance_name", "zone", "machine_type",
                "disk_type", "disk_gib", "status", "created_at", "stop_at",
            )},
            "disk_gib": 200,
        })

    monkeypatch.setattr(ledger.gce_identity, "read_existing_delhi_vm", mismatch)
    with pytest.raises(ledger.CostAdmissionDenied, match="GCE identity"):
        await imported(virgin)

    async def unavailable(_host):
        raise GCEIdentityError("gce_read_unavailable")

    monkeypatch.setattr(ledger.gce_identity, "read_existing_delhi_vm", unavailable)
    with pytest.raises(GCEIdentityError, match="gce_read_unavailable"):
        await imported(virgin)
    async with virgin.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_budget_accounts") == 0
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 0


@pytest.mark.asyncio
async def test_exact_replay_does_not_requery_provider(virgin, monkeypatch):
    item = evidence()
    first = await imported(virgin, item)

    async def unavailable(_host):
        raise GCEIdentityError("should_not_probe_on_replay")

    monkeypatch.setattr(ledger.gce_identity, "read_existing_delhi_vm", unavailable)
    replay = await imported(virgin, item)
    assert replay["id"] == first["id"]


async def _deployment_schema(pool):
    async with pool.acquire() as conn:
        for name in ("017_deployment_admission", "018_host_capacity"):
            await conn.execute(importlib.import_module("migrations.versions." + name).SCHEMA_SQL)
        await conn.execute("""CREATE TABLE coding_runs(
            id UUID PRIMARY KEY,user_id TEXT NOT NULL,status TEXT NOT NULL,
            source_egress_consented_at TIMESTAMPTZ,deleted_at TIMESTAMPTZ)""")
        for name in ("019_validation_capacity", "020_validation_results", "021_image_build_jobs"):
            await conn.execute(importlib.import_module("migrations.versions." + name).SCHEMA_SQL)


@pytest.mark.asyncio
async def test_imported_host_funds_capacity_and_not_second_vm_charge(virgin):
    item = evidence()
    parent = await imported(virgin, item)
    await _deployment_schema(virgin)
    host = host_capacity.FundedHost(
        pool_id="existing-delhi-host", project_id=item.project_id,
        server_uuid="test-existing-delhi-server", funding_reservation_id=parent["id"],
        funding_owner_user_id="operator", cpu_millis=2000, memory_mib=8192,
        storage_mib=20480, enforcement_hash="f" * 64,
    )
    await host_capacity.register_pool(virgin, host, evidence_reference="test:host-limit-proof")
    target = deploy.RegisteredTarget(
        target_id="existing-delhi-target", base_url="http://127.0.0.1:18000", team_id=0,
        inventory_secret_ref="test/inventory", configuration_secret_ref="test/configuration",
        deployment_secret_ref="test/deploy", recovery_secret_ref="test/recovery",
        project_id=item.project_id,
        server_uuid=host.server_uuid, server_id=1, destination_uuid="test-destination",
        destination_id=7, destination_type="App\\Models\\StandaloneDocker",
        destination_network="gca-app-existing-delhi-target",
        coolify_project_uuid="test-project",
        environment_uuid="test-environment", owner_user_id="alice", capacity_pool_id=host.pool_id,
    )
    await deploy.register_target(virgin, target, evidence_reference="test:target-proof")
    manifest = deploy.DeploymentManifest(
        request_id=uuid4(), target_id=target.target_id, target_hash=deploy.fingerprint(target),
        release_hash="a" * 64, compose_hash="b" * 64, validation_attestation_hash="c" * 64,
        estimated_upper_bound_paise=0,
        capacity=host_capacity.CapacityRequest(pool_id=host.pool_id, cpu_millis=1000,
                                              memory_mib=2048, storage_mib=4096),
        planned_end_at=item.stop_at,
    )
    await deploy.record_user_grant(
        virgin, manifest, user_id="alice", authority_reference="test:authenticated-approval",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    deployment = await deploy.enqueue(virgin, manifest, user_id="alice",
                                      idempotency_key="approved-existing-host-app")
    assert deployment["reservation_id"] is None
    assert await deploy.claim_dispatch(virgin, deployment["id"], user_id="alice",
                                       require_validation=False)
    assert await deploy.claim_dispatch(virgin, deployment["id"], user_id="alice",
                                       require_validation=False) is None
    async with virgin.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 1
        assert await conn.fetchval("SELECT estimated_paise FROM cloud_cost_reservations") == item.estimated_total_hosting_paise


@pytest.mark.asyncio
async def test_underfunded_import_cannot_back_host_capacity(virgin):
    item = evidence(remaining=1_000_000)
    parent = await imported(virgin, item)
    await _deployment_schema(virgin)
    host = host_capacity.FundedHost(
        pool_id="underfunded-host", project_id=item.project_id, server_uuid="test-server",
        funding_reservation_id=parent["id"], funding_owner_user_id="operator",
        cpu_millis=2000, memory_mib=8192, storage_mib=20480, enforcement_hash="f" * 64,
    )
    with pytest.raises(ledger.CostAdmissionDenied, match="active hosting obligation"):
        await host_capacity.register_pool(virgin, host, evidence_reference="test:not-funded")
    async with virgin.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM coding_host_capacity") == 0
