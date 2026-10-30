"""Real PostgreSQL locking tests, opt-in and restricted to loopback databases."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.coding import cost_ledger as ledger
from app.coding.cloud_budget import CloudCostRequest, CloudCreditSnapshot

DSN = os.getenv("COST_LEDGER_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set COST_LEDGER_TEST_DSN to local PostgreSQL")
PROJECT = "test-cost-project"


def request(end, amount=100_000, category="hosting"):
    return CloudCostRequest(project_id=PROJECT, category=category,
                            estimated_upper_bound_paise=amount, planned_end_at=end)


async def reserve(db, key="one", amount=100_000, user="alice", category="hosting"):
    pool, _, end = db
    return await ledger.reserve(pool, request(end, amount, category),
                                user_id=user, idempotency_key=key)


def identity(row, user="alice"):
    return {"project_id": PROJECT, "reservation_id": row["id"], "user_id": user}


@pytest.mark.asyncio
async def test_concurrent_cross_user_admission_cannot_overspend(db):
    results = await asyncio.gather(*[
        reserve(db, key=str(i), amount=400_000, user=f"user-{i}") for i in range(8)
    ], return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 3
    assert sum(isinstance(r, ledger.CostAdmissionDenied) for r in results) == 5
    async with db[0].acquire() as conn:
        assert await conn.fetchval("SELECT sum(estimated_paise) FROM cloud_cost_reservations") == 1_200_000
    # Hosting consumption does not take any of the Vertex envelope.
    assert (await reserve(db, key="vertex", amount=850_000, category="vertex"))["status"] == "reserved"


@pytest.mark.asyncio
async def test_duplicate_reservation_and_dispatch_are_atomic(db):
    rows = await asyncio.gather(*[reserve(db) for _ in range(8)])
    assert len({r["id"] for r in rows}) == 1
    claims = await asyncio.gather(*[
        ledger.start_dispatch(db[0], **identity(rows[0])) for _ in range(8)
    ])
    assert claims.count(True) == 1
    assert claims.count(False) == 7
    async with db[0].acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM cloud_cost_events WHERE event_type='dispatch_intent'"
        ) == 1


@pytest.mark.asyncio
async def test_reservation_and_audit_event_commit_together(db, monkeypatch):
    async def fail_event(*args, **kwargs):
        raise RuntimeError("simulated audit storage failure")

    monkeypatch.setattr(ledger, "_event", fail_event)
    with pytest.raises(RuntimeError, match="audit storage failure"):
        await reserve(db)
    async with db[0].acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cloud_cost_reservations") == 0


@pytest.mark.asyncio
async def test_failed_dispatch_transaction_grants_no_authority(db, monkeypatch):
    row = await reserve(db)

    async def fail_event(*args, **kwargs):
        raise RuntimeError("simulated commit boundary failure")

    monkeypatch.setattr(ledger, "_event", fail_event)
    with pytest.raises(RuntimeError, match="commit boundary failure"):
        await ledger.start_dispatch(db[0], **identity(row))
    async with db[0].acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM cloud_cost_reservations WHERE id=$1", row["id"],
        ) == "reserved"


@pytest.mark.asyncio
async def test_running_cost_accrual_is_not_double_counted_with_refreshed_balance(db):
    row = await reserve(db, amount=1_300_000)
    await ledger.start_dispatch(db[0], **identity(row))
    now = datetime.now(timezone.utc)
    await ledger.record_accrual(db[0], **identity(row), accrued_paise=100_000,
                               observed_at=now, evidence_reference="test:provider-usage")
    snapshot = db[1].model_copy(update={"observed_at": now, "remaining_credit_paise": 2_750_000})
    await ledger.refresh_credit(db[0], snapshot, evidence_reference="test:remaining-credit")
    # 100,000 spent + 1,200,000 still reserved leaves 100,000 in the hosting cap.
    assert (await reserve(db, key="remaining", amount=100_000))["status"] == "reserved"
    with pytest.raises(ledger.CostAdmissionDenied):
        await reserve(db, key="extra", amount=1)
    assert (await reserve(db, key="vertex", amount=850_000, category="vertex"))["status"] == "reserved"


@pytest.mark.asyncio
async def test_accrual_cannot_be_reversed_released_or_under_settled(db):
    row = await reserve(db)
    await ledger.start_dispatch(db[0], **identity(row))
    now = datetime.now(timezone.utc)
    args = dict(accrued_paise=50_000, observed_at=now, evidence_reference="test:metering")
    await ledger.record_accrual(db[0], **identity(row), **args)
    await ledger.record_accrual(db[0], **identity(row), **args)  # exact replay
    for changes in ({"accrued_paise": 49_999}, {"observed_at": now - timedelta(seconds=1)},
                    {"evidence_reference": "conflicting-evidence"}):
        with pytest.raises(ledger.CostLedgerConflict):
            await ledger.record_accrual(db[0], **identity(row), **(args | changes))
    with pytest.raises(ledger.CostLedgerConflict):
        await ledger.reconcile(db[0], **identity(row), outcome="released", evidence_reference="invalid")
    with pytest.raises(ledger.CostLedgerConflict):
        await ledger.reconcile(db[0], **identity(row), outcome="settled", actual_paise=49_999,
                               evidence_reference="invalid")
    settled = await ledger.reconcile(db[0], **identity(row), outcome="settled", actual_paise=50_000,
                                     evidence_reference="test:final-bill")
    assert settled["actual_paise"] == 50_000


@pytest.mark.asyncio
async def test_idempotency_binds_complete_request(db):
    await reserve(db)
    with pytest.raises(ledger.CostLedgerConflict):
        await reserve(db, amount=200_000)


@pytest.mark.asyncio
async def test_wrong_owner_or_project_cannot_dispatch_or_reconcile(db):
    row = await reserve(db)
    with pytest.raises(ledger.CostLedgerConflict):
        await ledger.start_dispatch(db[0], **identity(row, "bob"))
    with pytest.raises(ledger.CostLedgerConflict):
        await ledger.reconcile(db[0], **identity(row, "bob"), outcome="released",
                               evidence_reference="test:forged-other-user")
    with pytest.raises(ledger.CostAdmissionDenied):
        await ledger.start_dispatch(db[0], **{**identity(row), "project_id": "other-project"})


@pytest.mark.asyncio
async def test_uncertain_delivery_keeps_entire_reservation(db):
    row = await reserve(db, amount=1_400_000)
    assert await ledger.start_dispatch(db[0], **identity(row))
    await ledger.reconcile(db[0], **identity(row), outcome="uncertain",
                           evidence_reference="test:provider-timeout")
    assert not await ledger.start_dispatch(db[0], **identity(row))
    with pytest.raises(ledger.CostAdmissionDenied):
        await reserve(db, key="cannot-spend-uncertain-funds", amount=1)


@pytest.mark.asyncio
async def test_cancel_unstarted_and_release_idempotently(db):
    row = await reserve(db, amount=1_400_000)
    for _ in range(2):
        await ledger.reconcile(db[0], **identity(row), outcome="released",
                               evidence_reference="test:never-dispatched")
    assert not await ledger.start_dispatch(db[0], **identity(row))
    assert (await reserve(db, key="replacement", amount=1_400_000))["status"] == "reserved"
    with pytest.raises(ledger.CostLedgerConflict):
        await ledger.reconcile(db[0], **identity(row), outcome="settled", actual_paise=1,
                               evidence_reference="test:overwrite")


@pytest.mark.asyncio
async def test_settlement_returns_unused_estimate_not_actual_spend(db):
    row = await reserve(db, amount=1_400_000)
    await ledger.start_dispatch(db[0], **identity(row))
    for _ in range(2):
        await ledger.reconcile(db[0], **identity(row), outcome="settled", actual_paise=1_200_000,
                               evidence_reference="test:final-usage-no-liability")
    await reserve(db, key="remaining", amount=200_000)
    with pytest.raises(ledger.CostAdmissionDenied):
        await reserve(db, key="overspend", amount=1)


@pytest.mark.asyncio
async def test_real_overrun_is_recorded_and_blocks_more_spend(db):
    row = await reserve(db, amount=100_000)
    await ledger.start_dispatch(db[0], **identity(row))
    result = await ledger.reconcile(
        db[0], **identity(row), outcome="settled", actual_paise=1_500_000,
        evidence_reference="test:unexpected-provider-overrun",
    )
    assert result["actual_paise"] == 1_500_000
    with pytest.raises(ledger.CostAdmissionDenied):
        await reserve(db, key="next", amount=1)


@pytest.mark.asyncio
async def test_stale_evidence_blocks_dispatch_even_if_reservation_exists(db):
    row = await reserve(db)
    async with db[0].acquire() as conn:
        await conn.execute("UPDATE cloud_budget_accounts SET observed_at=now()-interval '2 hours'")
    with pytest.raises(ledger.CostAdmissionDenied, match="stale"):
        await ledger.start_dispatch(db[0], **identity(row))
    # Replaying a reservation is NOT a new dispatch authorization.
    assert (await reserve(db))["id"] == row["id"]


@pytest.mark.asyncio
async def test_balance_drop_rechecked_at_dispatch_and_preserves_vertex(db):
    row = await reserve(db)
    snapshot = db[1].model_copy(update={
        "remaining_credit_paise": 1_450_000,
        "observed_at": datetime.now(timezone.utc),
    })
    await ledger.refresh_credit(db[0], snapshot, evidence_reference="test:external-spend")
    with pytest.raises(ledger.CostAdmissionDenied):
        await ledger.start_dispatch(db[0], **identity(row))


@pytest.mark.asyncio
async def test_enrollment_cannot_reset_budget_or_deadlines(db):
    with pytest.raises(ledger.CostLedgerConflict):
        await ledger.register_account(db[0], db[1], hosting_ends_at=db[2],
                                      evidence_reference="test:reset")
    later = request(db[2] + timedelta(minutes=10))
    with pytest.raises(ledger.CostAdmissionDenied):
        await ledger.reserve(db[0], later, user_id="alice", idempotency_key="extension")


@pytest.mark.asyncio
async def test_future_reserve_and_invalid_settlements_are_rejected(db):
    with pytest.raises(ledger.CostAdmissionDenied):
        await reserve(db, category="future_reserve")
    row = await reserve(db)
    for amount in [-1, 1.5, True, None]:
        with pytest.raises(ValueError):
            await ledger.reconcile(db[0], **identity(row), outcome="settled",
                                   actual_paise=amount, evidence_reference="test:bad")
    with pytest.raises(ledger.CostLedgerConflict):
        await ledger.reconcile(db[0], **identity(row), outcome="settled", actual_paise=0,
                               evidence_reference="test:no-dispatch")
