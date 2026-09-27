"""Pure preflight for the approved credit envelopes; never a billing hard-stop claim.

All monetary inputs are integer paise. This evaluator does not reserve funds or perform
cloud operations. A future executor must atomically persist reservations and reconcile
actual provider charges before using an allowed decision.
"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CloudCreditSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    currency: Literal["INR"] = "INR"
    remaining_credit_paise: int = Field(ge=0, strict=True)
    observed_at: datetime
    expires_at: datetime
    hosting_spent_paise: int = Field(default=0, ge=0, strict=True)
    hosting_reserved_paise: int = Field(default=0, ge=0, strict=True)
    vertex_spent_paise: int = Field(default=0, ge=0, strict=True)
    vertex_reserved_paise: int = Field(default=0, ge=0, strict=True)

    @field_validator("observed_at", "expires_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("credit evidence must use timezone-aware timestamps")
        return value


class CloudCostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    category: Literal["hosting", "vertex", "future_reserve"]
    # Must include ancillary costs and the full requested lifetime, not just startup.
    estimated_upper_bound_paise: int = Field(gt=0, strict=True)
    planned_end_at: datetime

    @field_validator("planned_end_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("planned end must use a timezone-aware timestamp")
        return value


def evaluate_cloud_cost(
    snapshot: CloudCreditSnapshot,
    request: CloudCostRequest,
    *,
    now: datetime,
) -> dict:
    """Evaluate the explicitly approved 2026-09-19 reserve reallocation.

    Hosting may use at most INR 3,000 of the former INR 9,000 reserve for the
    initial 30-day VM term. Vertex's combined INR 8,500 is never a funding source.
    These are planning envelopes, not provider-enforced spending limits.
    """
    if now.utcoffset() is None:
        raise ValueError("now must use a timezone-aware timestamp")
    reasons = []
    if snapshot.project_id != request.project_id:
        reasons.append("project_mismatch")
    if request.category == "future_reserve":
        reasons.append("future_reserve_requires_new_authority")
    if snapshot.observed_at > now or now - snapshot.observed_at > timedelta(hours=1):
        reasons.append("credit_evidence_stale_or_future_dated")
    if snapshot.expires_at <= now or request.planned_end_at > snapshot.expires_at:
        reasons.append("credit_expires_before_workload_ends")
    if request.planned_end_at <= now:
        reasons.append("invalid_workload_lifetime")
    if request.category == "hosting" and request.planned_end_at > now + timedelta(days=30):
        reasons.append("hosting_term_requires_new_authority")

    caps = {"hosting": 1_400_000, "vertex": 850_000, "future_reserve": 0}
    spent = {
        "hosting": snapshot.hosting_spent_paise,
        "vertex": snapshot.vertex_spent_paise,
        "future_reserve": 0,
    }
    reserved = {
        "hosting": snapshot.hosting_reserved_paise,
        "vertex": snapshot.vertex_reserved_paise,
        "future_reserve": 0,
    }
    category_available = max(0, caps[request.category] - spent[request.category] - reserved[request.category])
    # Actual remaining credit already excludes settled spend. Also protect the
    # OTHER category's unspent, unreserved envelope if unrelated usage lowered
    # the balance. A hosting request must never eat Vertex's unreserved budget.
    other_category = "vertex" if request.category == "hosting" else "hosting"
    other_unreserved = max(
        0, caps[other_category] - spent[other_category] - reserved[other_category],
    )
    total_available = max(
        0, snapshot.remaining_credit_paise - 600_000
        - snapshot.hosting_reserved_paise - snapshot.vertex_reserved_paise
        - other_unreserved,
    )
    if request.estimated_upper_bound_paise > category_available:
        reasons.append("category_envelope_exceeded")
    if request.estimated_upper_bound_paise > total_available:
        reasons.append("remaining_credit_or_protected_reserve_exceeded")
    return {
        "policy_version": "cloud-credit-envelopes-2026-09-19-hosting-reserve-only",
        "preflight_passed": not reasons,
        "reasons": reasons,
        "category_available_paise": category_available,
        "total_available_paise": total_available,
        "currency": "INR",
        "protected_future_reserve_paise": 600_000,
        "vertex_envelope_paise": caps["vertex"],
        "funds_reserved": False,
        "cloud_operation_authorized": False,
        "next_requirement": "trusted_cost_evidence_and_atomic_reservation",
    }
