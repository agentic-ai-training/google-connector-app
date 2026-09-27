"""Trusted-controller cost ledger. Not a public API or a provider billing cap.

One account row lock serializes admission across ALL users of a funded project.
Network calls must happen outside transactions, after start_dispatch returns True.
If delivery is ambiguous, never repeat it: retain funds until provider reconciliation.
Only a trusted operator/provider adapter may register evidence or settle costs.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.coding.cloud_budget import CloudCostRequest, CloudCreditSnapshot, evaluate_cloud_cost

POLICY = "cloud-credit-envelopes-2026-09-19-hosting-reserve-only"


class CostLedgerConflict(RuntimeError):
    pass


class CostAdmissionDenied(RuntimeError):
    pass


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("identity/evidence must be a nonempty bounded string")
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _event(conn, project, kind, reservation=None, evidence=None):
    await conn.execute(
        "INSERT INTO cloud_cost_events(project_id,reservation_id,event_type,evidence_reference) "
        "VALUES($1,$2,$3,$4)", project, reservation, kind, evidence,
    )


async def register_account(pool, snapshot: CloudCreditSnapshot, *,
                           hosting_ends_at: datetime, evidence_reference: str) -> None:
    """Operator-only enrollment; never reset an existing account or import old spend as zero."""
    _text(evidence_reference)
    now = _now()
    probe = CloudCostRequest(project_id=snapshot.project_id, category="hosting",
                             estimated_upper_bound_paise=1, planned_end_at=hosting_ends_at)
    if not evaluate_cloud_cost(snapshot, probe, now=now)["preflight_passed"]:
        raise CostAdmissionDenied("invalid initial credit evidence or hosting deadline")
    if any((snapshot.hosting_spent_paise, snapshot.hosting_reserved_paise,
            snapshot.vertex_spent_paise, snapshot.vertex_reserved_paise)):
        raise CostAdmissionDenied("existing obligations require explicit audited ledger import")
    async with pool.acquire() as conn, conn.transaction():
        result = await conn.fetchval(
            """INSERT INTO cloud_budget_accounts
            (project_id,policy_version,opening_credit_paise,observed_credit_paise,
             observed_at,credit_expires_at,hosting_ends_at,evidence_reference)
            VALUES($1,$2,$3,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING RETURNING project_id""",
            snapshot.project_id, POLICY, snapshot.remaining_credit_paise,
            snapshot.observed_at, snapshot.expires_at, hosting_ends_at, evidence_reference,
        )
        if result is None:
            raise CostLedgerConflict("account already enrolled; its ledger cannot be reset")
        await _event(conn, snapshot.project_id, "account_enrolled", evidence=evidence_reference)


async def _account(conn, project):
    row = await conn.fetchrow(
        "SELECT * FROM cloud_budget_accounts WHERE project_id=$1 FOR UPDATE", project,
    )
    if not row or row["policy_version"] != POLICY:
        raise CostAdmissionDenied("missing account or unsupported allocation policy")
    return row


async def refresh_credit(pool, snapshot: CloudCreditSnapshot, *, evidence_reference: str):
    """Record fresh operator/provider evidence, without resetting caps/spend/deadlines."""
    _text(evidence_reference)
    now = _now()
    if not now - timedelta(hours=1) <= snapshot.observed_at <= now:
        raise CostAdmissionDenied("credit evidence is stale or future-dated")
    async with pool.acquire() as conn, conn.transaction():
        account = await _account(conn, snapshot.project_id)
        if snapshot.observed_at < account["observed_at"]:
            raise CostLedgerConflict("older credit observation")
        if snapshot.expires_at != account["credit_expires_at"]:
            raise CostLedgerConflict("credit expiry change requires new authority")
        await conn.execute(
            "UPDATE cloud_budget_accounts SET observed_credit_paise=$2,observed_at=$3,"
            "evidence_reference=$4 WHERE project_id=$1", snapshot.project_id,
            snapshot.remaining_credit_paise, snapshot.observed_at, evidence_reference,
        )
        await _event(conn, snapshot.project_id, "credit_observed", evidence=evidence_reference)


async def _admit(conn, account, request, *, exclude=None):
    rows = await conn.fetch(
        """SELECT category,
        COALESCE(sum(CASE WHEN status='settled' THEN actual_paise ELSE accrued_paise END),0)::bigint AS spent,
        COALESCE(sum(GREATEST(0,estimated_paise-accrued_paise)) FILTER
          (WHERE status IN ('reserved','dispatched','uncertain') AND
          ($2::uuid IS NULL OR id<>$2)),0)::bigint AS reserved
        FROM cloud_cost_reservations WHERE project_id=$1 GROUP BY category""",
        request.project_id, exclude,
    )
    counts = {f"{r['category']}_{field}_paise": r[field]
              for r in rows for field in ("spent", "reserved")}
    spent = sum(r["spent"] for r in rows)
    snapshot = CloudCreditSnapshot(
        project_id=request.project_id,
        # Conservative when provider billing lags; never replenish initial authority.
        remaining_credit_paise=max(0, min(account["observed_credit_paise"],
                                         account["opening_credit_paise"] - spent)),
        observed_at=account["observed_at"], expires_at=account["credit_expires_at"],
        **counts,
    )
    result = evaluate_cloud_cost(snapshot, request, now=_now())
    if request.category == "hosting" and request.planned_end_at > account["hosting_ends_at"]:
        result["reasons"].append("absolute_hosting_deadline_exceeded")
    if result["reasons"]:
        raise CostAdmissionDenied(",".join(result["reasons"]))


async def reserve(pool, request: CloudCostRequest, *, user_id: str, idempotency_key: str):
    async with pool.acquire() as conn, conn.transaction():
        return await reserve_in_transaction(conn, request, user_id=user_id,
                                            idempotency_key=idempotency_key)


async def reserve_in_transaction(conn, request: CloudCostRequest, *, user_id: str,
                                 idempotency_key: str):
    """Trusted controller composition: reservation and deployment must commit together."""
    if not conn.is_in_transaction():
        raise CostLedgerConflict("reservation requires an explicit transaction")
    _text(user_id)
    _text(idempotency_key)
    if request.category == "future_reserve":
        raise CostAdmissionDenied("future reserve requires new authority")
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    account = await _account(conn, request.project_id)
    row = await conn.fetchrow(
        "SELECT * FROM cloud_cost_reservations WHERE project_id=$1 AND user_id=$2 "
        "AND idempotency_key=$3", request.project_id, user_id, idempotency_key,
    )
    if row:
        if row["request_hash"] != digest:
            raise CostLedgerConflict("idempotency key belongs to a different cost request")
        return dict(row)
    await _admit(conn, account, request)
    row = await conn.fetchrow(
        """INSERT INTO cloud_cost_reservations
        (id,project_id,user_id,idempotency_key,request_hash,category,estimated_paise,planned_end_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""",
        uuid4(), request.project_id, user_id, idempotency_key, digest,
        request.category, request.estimated_upper_bound_paise, request.planned_end_at,
    )
    await _event(conn, request.project_id, "reserved", row["id"])
    return dict(row)


async def _owned(conn, project, reservation_id, user):
    row = await conn.fetchrow(
        "SELECT * FROM cloud_cost_reservations WHERE project_id=$1 AND id=$2 AND user_id=$3",
        project, UUID(str(reservation_id)), _text(user),
    )
    if not row:
        raise CostLedgerConflict("reservation not found for this owner/project")
    return row


async def start_dispatch(pool, *, project_id, reservation_id, user_id) -> bool:
    """Exactly one caller receives dispatch authority; false means reconcile, NOT retry."""
    async with pool.acquire() as conn, conn.transaction():
        return await dispatch_in_transaction(conn, project_id=project_id,
                                              reservation_id=reservation_id, user_id=user_id)


async def dispatch_in_transaction(conn, *, project_id, reservation_id, user_id) -> bool:
    if not conn.is_in_transaction():
        raise CostLedgerConflict("dispatch requires an explicit transaction")
    account = await _account(conn, project_id)
    row = await _owned(conn, project_id, reservation_id, user_id)
    if row["status"] != "reserved":
        return False
    request = CloudCostRequest(
        project_id=project_id, category=row["category"],
        estimated_upper_bound_paise=row["estimated_paise"],
        planned_end_at=row["planned_end_at"],
    )
    await _admit(conn, account, request, exclude=row["id"])
    await conn.execute(
        "UPDATE cloud_cost_reservations SET status='dispatched',updated_at=now() WHERE id=$1",
        row["id"],
    )
    await _event(conn, project_id, "dispatch_intent", row["id"])
    return True


async def reconcile(pool, *, project_id, reservation_id, user_id,
                    outcome: str, evidence_reference: str, actual_paise: int | None = None):
    """Trusted reconciliation only. Never free funds merely because a lease/time limit expired.

    'released' means provider evidence proves zero incurred AND zero remaining liability;
    stopping a VM while retaining its disk is not sufficient evidence.
    Over-estimate actual charges are recorded, not rejected or hidden to pass a cap.
    """
    _text(evidence_reference)
    if outcome not in {"uncertain", "settled", "released"}:
        raise ValueError("invalid outcome")
    if outcome == "settled":
        if type(actual_paise) is not int or actual_paise < 0:
            raise ValueError("actual amount must be nonnegative integer paise")
    elif actual_paise is not None:
        raise ValueError("only settlement has actual charges")
    async with pool.acquire() as conn, conn.transaction():
        await _account(conn, project_id)
        row = await _owned(conn, project_id, reservation_id, user_id)
        if row["status"] in {"settled", "released"}:
            if (row["status"], row["actual_paise"], row["evidence_reference"]) != (
                    outcome, actual_paise, evidence_reference):
                raise CostLedgerConflict("terminal reconciliation cannot be overwritten")
            return dict(row)
        if row["status"] == "reserved" and outcome != "released":
            raise CostLedgerConflict("no dispatch intent to reconcile")
        if outcome == "released" and row["accrued_paise"]:
            raise CostLedgerConflict("incurred charges must be settled, not released")
        if outcome == "settled" and actual_paise < row["accrued_paise"]:
            raise CostLedgerConflict("settlement cannot erase recorded accrued charges")
        row = await conn.fetchrow(
            """UPDATE cloud_cost_reservations SET status=$2,actual_paise=$3,
            evidence_reference=$4,updated_at=now() WHERE id=$1 RETURNING *""",
            row["id"], outcome, actual_paise, evidence_reference,
        )
        await _event(conn, project_id, outcome, row["id"], evidence_reference)
        return dict(row)


async def record_accrual(pool, *, project_id, reservation_id, user_id,
                         accrued_paise: int, observed_at: datetime, evidence_reference: str):
    """Trusted cumulative provider charge evidence for a still-running obligation.

    Move incurred usage from reserved to spent without freeing the original estimate.
    Overruns are retained honestly; corrections/refunds require separately audited authority.
    """
    _text(evidence_reference)
    if type(accrued_paise) is not int or accrued_paise < 0:
        raise ValueError("accrued amount must be nonnegative integer paise")
    if observed_at.utcoffset() is None or observed_at > _now():
        raise ValueError("accrual timestamp must be timezone-aware and not future-dated")
    async with pool.acquire() as conn, conn.transaction():
        await _account(conn, project_id)
        row = await _owned(conn, project_id, reservation_id, user_id)
        if row["status"] not in {"dispatched", "uncertain"}:
            raise CostLedgerConflict("only outstanding dispatched costs can accrue")
        if accrued_paise < row["accrued_paise"]:
            raise CostLedgerConflict("cumulative accrued charges cannot decrease")
        if row["accrual_observed_at"] and observed_at < row["accrual_observed_at"]:
            raise CostLedgerConflict("older accrual evidence cannot overwrite newer evidence")
        if row["accrual_observed_at"] == observed_at:
            if (row["accrued_paise"], row["accrual_evidence_reference"]) != (accrued_paise, evidence_reference):
                raise CostLedgerConflict("conflicting evidence for the same observation time")
            return dict(row)
        updated = await conn.fetchrow(
            """UPDATE cloud_cost_reservations SET accrued_paise=$2,accrual_observed_at=$3,
            accrual_evidence_reference=$4,updated_at=now() WHERE id=$1 RETURNING *""",
            row["id"], accrued_paise, observed_at, evidence_reference,
        )
        await _event(conn, project_id, "charges_accrued", row["id"], evidence_reference)
        return dict(updated)
