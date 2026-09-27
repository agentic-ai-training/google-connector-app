from datetime import datetime, timedelta, timezone

import pytest

from app.coding.cloud_budget import (
    CloudCostRequest, CloudCreditSnapshot, evaluate_cloud_cost,
)


NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def evaluate(category="hosting", amount=1_400_000, **snapshot_changes):
    snapshot = CloudCreditSnapshot(**{
        "project_id": "test-project", "remaining_credit_paise": 2_850_000,
        "observed_at": NOW, "expires_at": NOW + timedelta(days=60),
        **snapshot_changes,
    })
    request = CloudCostRequest(
        project_id="test-project", category=category,
        estimated_upper_bound_paise=amount, planned_end_at=NOW + timedelta(days=30),
    )
    return evaluate_cloud_cost(snapshot, request, now=NOW)


def test_approved_hosting_envelope_is_not_spend_authority():
    result = evaluate()
    assert result["preflight_passed"]
    assert not result["funds_reserved"]
    assert not result["cloud_operation_authorized"]


@pytest.mark.parametrize("category,amount", [("hosting", 1_400_001), ("vertex", 850_001)])
def test_envelopes_cannot_borrow_from_each_other(category, amount):
    assert "category_envelope_exceeded" in evaluate(category, amount)["reasons"]


def test_vertex_tuning_evaluation_and_inference_share_one_cap():
    result = evaluate("vertex", 100_001, vertex_spent_paise=700_000, vertex_reserved_paise=50_000)
    assert not result["preflight_passed"]
    assert result["category_available_paise"] == 100_000


def test_future_reserve_cannot_be_spent():
    assert "future_reserve_requires_new_authority" in evaluate("future_reserve", 1)["reasons"]


def test_actual_remaining_credit_overrides_initial_plan():
    result = evaluate(amount=200_001, remaining_credit_paise=1_100_000)
    assert "remaining_credit_or_protected_reserve_exceeded" in result["reasons"]


def test_existing_commitments_reduce_available_credit():
    assert not evaluate(hosting_reserved_paise=1)["preflight_passed"]


@pytest.mark.parametrize("age", [timedelta(hours=-2), timedelta(minutes=1)])
def test_credit_evidence_must_be_recent_and_not_future_dated(age):
    assert not evaluate(observed_at=NOW + age)["preflight_passed"]


def test_month_of_hosting_cannot_outlive_credit():
    result = evaluate(expires_at=NOW + timedelta(days=10))
    assert "credit_expires_before_workload_ends" in result["reasons"]


def test_timezone_and_integer_currency_are_required():
    with pytest.raises(ValueError):
        evaluate(observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        evaluate(remaining_credit_paise=28_500.5)
    with pytest.raises(ValueError):
        evaluate(hosting_spent_paise=-1)


def test_project_identity_cannot_be_substituted():
    assert "project_mismatch" in evaluate(project_id="other-project")["reasons"]


def test_hosting_increase_uses_only_authorized_reserve():
    result = evaluate()
    assert result["category_available_paise"] == 1_400_000
    assert result["protected_future_reserve_paise"] == 600_000
    assert result["vertex_envelope_paise"] == 850_000
    assert result["total_available_paise"] == 1_400_000


def test_lower_balance_cannot_spend_unreserved_vertex_allocation():
    result = evaluate(amount=100_001, remaining_credit_paise=1_550_000)
    assert result["total_available_paise"] == 100_000
    assert not result["preflight_passed"]


def test_vertex_cannot_spend_unreserved_hosting_allocation():
    result = evaluate("vertex", 100_001, remaining_credit_paise=2_100_000)
    assert result["total_available_paise"] == 100_000
    assert not result["preflight_passed"]


def test_other_spent_and_reserved_are_not_double_counted():
    result = evaluate(
        remaining_credit_paise=2_750_000,
        vertex_spent_paise=100_000, vertex_reserved_paise=200_000,
    )
    assert result["preflight_passed"]
    assert result["total_available_paise"] == 1_400_000


def test_hosting_extension_requires_new_authority():
    snapshot = CloudCreditSnapshot(
        project_id="test-project", remaining_credit_paise=2_850_000,
        observed_at=NOW, expires_at=NOW + timedelta(days=60),
    )
    request = CloudCostRequest(
        project_id="test-project", category="hosting", estimated_upper_bound_paise=1,
        planned_end_at=NOW + timedelta(days=31),
    )
    assert "hosting_term_requires_new_authority" in evaluate_cloud_cost(
        snapshot, request, now=NOW,
    )["reasons"]
