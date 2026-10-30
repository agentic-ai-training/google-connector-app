from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.coding.cloud_budget import CloudCreditSnapshot
from app.coding.credit_planning import CreditPlanningInput, project_credit_scenario


ANCHOR = datetime(2026, 10, 5, tzinfo=timezone.utc)
STOP = datetime(2026, 10, 19, 17, 24, 32, tzinfo=timezone.utc)


def scenario(**changes):
    return CreditPlanningInput(**{
        "project_id": "test-project",
        "evidence_kind": "operator_report_unknown_capture_time",
        "reported_remaining_paise": 2_161_067,
        "assumed_charge_anchor": ANCHOR,
        "hosting_stop_at": STOP,
        "daily_allowance_paise": 50_000,
        "uncertainty_reserve_paise": 50_000,
        "source_reference": "test:operator-report",
        **changes,
    })


def test_scenario_is_not_verified_evidence_or_authority():
    result = project_credit_scenario(scenario(), now=ANCHOR + timedelta(days=8))
    assert result["planning_balance_now_paise"] == 1_711_067
    assert result["planning_balance_at_or_after_stop_paise"] == 1_374_798
    assert result["projected_protected_allocation_shortfall_paise"] == 75_202
    for key in ("provider_balance_verified", "guaranteed_lower_bound", "funds_reserved",
                "cloud_operation_authorized", "ledger_admission_allowed"):
        assert result[key] is False
    assert "observed_at" not in result and "observed_at" not in result["source"]
    assert result["protected_vertex_paise"] == 850_000
    assert result["protected_future_reserve_paise"] == 600_000
    assert result["hosting_term_ceiling_paise"] == 1_400_000


def test_estimate_cannot_be_registered_as_a_verified_credit_snapshot():
    result = project_credit_scenario(scenario(), now=ANCHOR)
    with pytest.raises(ValidationError):
        CloudCreditSnapshot.model_validate(result)
    with pytest.raises(ValidationError):
        CloudCreditSnapshot.model_validate(scenario().model_dump())


def test_partial_second_rounds_debit_up_not_balance_up():
    result = project_credit_scenario(scenario(), now=ANCHOR + timedelta(microseconds=1))
    assert result["planning_balance_now_paise"] == 2_111_066


def test_balances_floor_at_zero_and_stop_does_not_hide_disk_liability():
    result = project_credit_scenario(scenario(), now=STOP + timedelta(days=100))
    assert result["planning_balance_now_paise"] == 0
    assert result["planning_balance_at_or_after_stop_paise"] == 0
    assert result["compute_deadline_elapsed"]
    assert result["unknown_usage_may_reduce_balance_further"]


@pytest.mark.parametrize("changes", [
    {"reported_remaining_paise": -1}, {"reported_remaining_paise": 1.5},
    {"reported_remaining_paise": True}, {"daily_allowance_paise": 0},
    {"daily_allowance_paise": True}, {"uncertainty_reserve_paise": 0},
    {"evidence_kind": "provider_verified"}, {"observed_at": ANCHOR},
    {"assumed_charge_anchor": ANCHOR.replace(tzinfo=None)},
    {"hosting_stop_at": STOP.replace(tzinfo=None)}, {"source_reference": ""},
])
def test_invalid_or_forged_inputs_rejected(changes):
    with pytest.raises(ValidationError):
        scenario(**changes)


@pytest.mark.parametrize("now", [ANCHOR - timedelta(seconds=1), ANCHOR.replace(tzinfo=None)])
def test_invalid_current_time_rejected(now):
    with pytest.raises(ValueError):
        project_credit_scenario(scenario(), now=now)


def test_stop_before_anchor_rejected():
    with pytest.raises(ValueError):
        project_credit_scenario(scenario(hosting_stop_at=ANCHOR), now=ANCHOR)
