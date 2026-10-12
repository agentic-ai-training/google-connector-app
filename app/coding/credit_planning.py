"""Integer-only credit scenarios, deliberately separate from spending evidence.

A stale report with an unknown capture time cannot prove a current balance.
These scenarios support preparation, not ledger enrollment, reservations or
provider writes. Unknown usage outside the chosen allowance is not bounded here.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreditPlanningInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    evidence_kind: Literal["operator_report_unknown_capture_time"]
    reported_remaining_paise: int = Field(ge=0, strict=True)
    assumed_charge_anchor: datetime
    hosting_stop_at: datetime
    daily_allowance_paise: int = Field(gt=0, strict=True)
    uncertainty_reserve_paise: int = Field(gt=0, strict=True)
    source_reference: str = Field(min_length=1, max_length=512)

    @field_validator("assumed_charge_anchor", "hosting_stop_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value.utcoffset() is None:
            raise ValueError("scenario timestamps must be timezone-aware")
        return value


def project_credit_scenario(value: CreditPlanningInput, *, now: datetime) -> dict:
    """Round assumed debits UP, balances DOWN; never manufacture observed_at.

    The allowance continues beyond VM STOP because persistent storage and other
    services can still accrue charges. STOP is a compute deadline, not a billing
    stop. An estimate is neither a guaranteed lower bound nor provider evidence.
    """
    if (now.utcoffset() is None or now < value.assumed_charge_anchor
            or value.hosting_stop_at <= value.assumed_charge_anchor):
        raise ValueError("invalid scenario timeline")

    def at(moment):
        delta = moment - value.assumed_charge_anchor
        # Integer microseconds avoid float rounding at a monetary boundary.
        micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        denominator = 86400 * 1_000_000
        debit = (micros * value.daily_allowance_paise + denominator - 1) // denominator
        return max(0, value.reported_remaining_paise - debit - value.uncertainty_reserve_paise)

    current = at(now)
    stop_balance = at(max(now, value.hosting_stop_at))
    protected = 850_000 + 600_000
    return {
        "schema": "credit-planning-scenario-v1",
        "project_id": value.project_id,
        "currency": "INR",
        "calculated_at": now.isoformat(),
        "source": value.model_dump(mode="json"),
        "planning_balance_now_paise": current,
        "planning_balance_at_or_after_stop_paise": stop_balance,
        "protected_vertex_paise": 850_000,
        "protected_future_reserve_paise": 600_000,
        "hosting_term_ceiling_paise": 1_400_000,
        "projected_protected_allocation_shortfall_paise": max(0, protected - stop_balance),
        "compute_deadline_elapsed": now >= value.hosting_stop_at,
        "provider_balance_verified": False,
        "guaranteed_lower_bound": False,
        "funds_reserved": False,
        "cloud_operation_authorized": False,
        "ledger_admission_allowed": False,
        "unknown_usage_may_reduce_balance_further": True,
        "next_action": "continue_non_spending_preparation_and_reconcile_before_new_liability",
    }
