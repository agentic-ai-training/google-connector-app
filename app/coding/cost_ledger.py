"""Trusted-controller cost ledger. Not a public API or a provider billing cap.

One account row lock serializes admission across ALL users of a funded project.
Provider reads must happen outside transactions. New provider writes may occur only
after start_dispatch returns True; existing-resource import makes no provider write.
If delivery is ambiguous, never repeat it: retain funds until provider reconciliation.
Only a trusted operator/provider adapter may register evidence or settle costs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.coding.cloud_budget import CloudCostRequest, CloudCreditSnapshot, evaluate_cloud_cost
from app.coding import gce_identity
from app.coding.validation_transport import IAPValidationHost

POLICY = "cloud-credit-envelopes-2026-09-19-hosting-reserve-only"
# The pre-VM confirmation rounded this amount to INR 28,694. The later Billing
# report supplied the exact promotional-credit total: INR 28,693.88.
INITIAL_TRIAL_CREDIT_PAISE = 2_869_388


class CostLedgerConflict(RuntimeError):
    pass


class CostAdmissionDenied(RuntimeError):
    pass


class ExistingDelhiVM(BaseModel):
    """Operator/provider-attested identity and bill for the one already-running VM."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: Literal["project-ee0c1944-144c-4364-ad4"]
    instance_id: Literal["822115653245959101"]
    instance_name: Literal["gca-coolify-delhi-01"]
    zone: Literal["asia-south2-a"]
    machine_type: Literal["e2-standard-4"]
    disk_type: Literal["pd-balanced"]
    disk_gib: Literal[100]
    status: Literal["RUNNING"]
    created_at: datetime
    observed_at: datetime
    stop_at: datetime
    initial_trial_credit_paise: Literal[2_869_388]
    remaining_trial_credit_paise: int = Field(ge=0, le=INITIAL_TRIAL_CREDIT_PAISE, strict=True)
    credit_observed_at: datetime
    # The portal gives a date but not an exact hour. Use a deliberately earlier
    # admission cutoff, never an invented precise provider expiration timestamp.
    credit_admission_cutoff: datetime
    accrued_hosting_paise: int = Field(gt=0, strict=True)
    charge_observed_at: datetime
    estimated_total_hosting_paise: int = Field(gt=0, strict=True)
    identity_evidence_reference: str = Field(min_length=1, max_length=512)
    credit_evidence_reference: str = Field(min_length=1, max_length=512)
    charge_evidence_reference: str = Field(min_length=1, max_length=512)

    @field_validator("created_at", "observed_at", "stop_at", "credit_observed_at",
                     "credit_admission_cutoff", "charge_observed_at")
    @classmethod
    def utc_dates(cls, value):
        if value.utcoffset() is None:
            raise ValueError("audited VM dates must be timezone-aware")
        return value.astimezone(timezone.utc)


def _import_preflight(evidence: ExistingDelhiVM):
    now = _now()
    created = datetime(2026, 9, 19, 17, 24, 34, 872000, timezone.utc)
    stop = datetime(2026, 10, 19, 17, 24, 32, tzinfo=timezone.utc)
    cutoff = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
    if (evidence.created_at != created or evidence.stop_at != stop
            or evidence.credit_admission_cutoff != cutoff
            or not evidence.created_at < now < stop
            or evidence.estimated_total_hosting_paise > 1_400_000
            or evidence.observed_at < now - timedelta(hours=1)
            or evidence.credit_observed_at < now - timedelta(hours=1)
            or evidence.charge_observed_at < now - timedelta(hours=1)
            or max(evidence.observed_at, evidence.credit_observed_at,
                   evidence.charge_observed_at) > now):
        raise CostAdmissionDenied("VM identity, budget, or current evidence is not valid")
    for reference in (evidence.identity_evidence_reference, evidence.credit_evidence_reference,
                      evidence.charge_evidence_reference):
        _text(reference)
    remainder = max(1, evidence.estimated_total_hosting_paise - evidence.accrued_hosting_paise)
    snapshot = CloudCreditSnapshot(
        project_id=evidence.project_id, remaining_credit_paise=evidence.remaining_trial_credit_paise,
        observed_at=evidence.credit_observed_at, expires_at=cutoff,
        hosting_spent_paise=evidence.accrued_hosting_paise,
    )
    request = CloudCostRequest(project_id=evidence.project_id, category="hosting",
                               estimated_upper_bound_paise=remainder, planned_end_at=stop)
    # The import records a pre-existing liability even if underfunded. A failing
    # preflight marks it uncertain and refuses new generated workloads.
    return evaluate_cloud_cost(snapshot, request, now=now)["preflight_passed"]


async def import_existing_delhi_vm(pool, evidence: ExistingDelhiVM, *, owner_user_id: str,
                                   operator_host: IAPValidationHost):
    """Atomic operator-only import; makes no cloud write and never creates a new VM.

    The local ledger must be virgin for this GCP project. Evidence must come from a
    current provider identity read, fresh trial balance and attributable cost record.
    The supplied references are audit handles, not substitutes for those checks.
    """
    _text(owner_user_id)
    digest = hashlib.sha256(json.dumps(evidence.model_dump(mode="json"), sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    key = "existing-gce-vm-" + evidence.instance_id
    # Idempotent replay needs neither a new provider call nor fresh billing data.
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT project_id FROM cloud_budget_accounts WHERE project_id=$1", evidence.project_id,
        )
        if existing:
            row = await conn.fetchrow(
                "SELECT * FROM cloud_cost_reservations WHERE project_id=$1 "
                "AND user_id=$2 AND idempotency_key=$3", evidence.project_id,
                owner_user_id, key,
            )
            if row and row["request_hash"] == digest:
                return dict(row)
            raise CostLedgerConflict("account already enrolled; import cannot reset its ledger")
    funded = _import_preflight(evidence)
    # Provider I/O must never hold a database lock or transaction open.
    observed = await gce_identity.read_existing_delhi_vm(operator_host)
    for field in ("project_id", "instance_id", "instance_name", "zone", "machine_type",
                  "disk_type", "disk_gib", "status", "created_at", "stop_at"):
        if getattr(observed, field) != getattr(evidence, field):
            raise CostAdmissionDenied("GCE identity does not match the import evidence")
    async with pool.acquire() as conn, conn.transaction():
        lock = int.from_bytes(hashlib.sha256(evidence.project_id.encode()).digest()[:8],
                              byteorder="big", signed=True)
        await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", lock)
        existing = await conn.fetchrow(
            "SELECT * FROM cloud_budget_accounts WHERE project_id=$1 FOR UPDATE",
            evidence.project_id,
        )
        if existing:
            row = await conn.fetchrow(
                "SELECT * FROM cloud_cost_reservations WHERE project_id=$1 "
                "AND user_id=$2 AND idempotency_key=$3", evidence.project_id,
                owner_user_id, key,
            )
            if row and row["request_hash"] == digest:
                return dict(row)
            raise CostLedgerConflict("account already enrolled; import cannot reset its ledger")
        funded = _import_preflight(evidence)
        account = await conn.fetchval(
            """INSERT INTO cloud_budget_accounts
            (project_id,policy_version,opening_credit_paise,observed_credit_paise,observed_at,
             credit_expires_at,hosting_ends_at,evidence_reference)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING project_id""",
            evidence.project_id, POLICY, evidence.initial_trial_credit_paise,
            evidence.remaining_trial_credit_paise, evidence.credit_observed_at,
            evidence.credit_admission_cutoff, evidence.stop_at,
            evidence.credit_evidence_reference,
        )
        if account != evidence.project_id:
            raise CostLedgerConflict("VM import account mismatch")
        await _event(conn, account, "existing_account_imported",
                     evidence=evidence.credit_evidence_reference)
        status = "dispatched" if funded else "uncertain"
        row = await conn.fetchrow(
            """INSERT INTO cloud_cost_reservations
            (id,project_id,user_id,idempotency_key,request_hash,category,estimated_paise,
             planned_end_at,status,accrued_paise,accrual_observed_at,accrual_evidence_reference,
             evidence_reference)
            VALUES($1,$2,$3,$4,$5,'hosting',$6,$7,$8,$9,$10,$11,$12) RETURNING *""",
            uuid4(), account, owner_user_id, key, digest,
            evidence.estimated_total_hosting_paise, evidence.stop_at, status,
            evidence.accrued_hosting_paise, evidence.charge_observed_at,
            evidence.charge_evidence_reference, evidence.identity_evidence_reference,
        )
        await _event(conn, account,
                     "existing_vm_imported" if funded else "existing_vm_imported_underfunded",
                     row["id"], evidence.identity_evidence_reference)
        return dict(row)


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
