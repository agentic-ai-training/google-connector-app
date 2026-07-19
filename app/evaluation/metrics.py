from __future__ import annotations

from statistics import mean
from typing import Any


METRIC_NAMES = (
    "task_success", "plan_correctness", "tool_correctness",
    "artifact_correctness", "recovery_success", "side_effect_integrity",
    "user_satisfaction", "retrieval_quality", "latency_ms", "tokens",
)


def evaluate_plan(plan, expected: dict[str, Any]) -> dict[str, float]:
    """Score independently observable plan qualities; do not hide trade-offs."""
    actual_services = list(plan.services)
    expected_services = list(expected.get("services", []))
    expected_operations = list(expected.get("operations", []))
    actual_pairs = [(step.service, step.operation) for step in plan.steps]
    expected_pairs = list(zip(expected_services, expected_operations))
    service_recall = (
        len(set(actual_services) & set(expected_services)) / max(1, len(set(expected_services)))
    )
    service_precision = (
        len(set(actual_services) & set(expected_services)) / max(1, len(set(actual_services)))
    )
    operation_accuracy = (
        sum(left == right for left, right in zip(actual_pairs, expected_pairs))
        / max(1, len(expected_pairs))
    )
    order_accuracy = 1.0 if actual_pairs == expected_pairs else 0.0
    dependencies_valid = all(
        all(dependency in {prior.id for prior in plan.steps[:index]}
            for dependency in step.dependencies)
        for index, step in enumerate(plan.steps)
    )
    max_tokens = float(expected.get("max_tokens", 1500 * max(1, len(plan.steps))))
    cost_score = min(1.0, max_tokens / max(1.0, float(plan.estimated_max_tokens)))
    return {
        "service_recall": round(service_recall, 4),
        "service_precision": round(service_precision, 4),
        "operation_accuracy": round(operation_accuracy, 4),
        "order_accuracy": order_accuracy,
        "dependency_validity": float(dependencies_valid),
        "cost_efficiency": round(cost_score, 4),
        "plan_correctness": round(mean([
            service_recall, service_precision, operation_accuracy,
            order_accuracy, float(dependencies_valid),
        ]), 4),
    }


def compare_policy_metrics(
    baseline: dict[str, float], candidate: dict[str, float], *, sample_size: int,
    minimum_samples: int = 30,
) -> dict[str, Any]:
    """Return auditable per-objective deltas; never create one opaque reward."""
    common = sorted(set(baseline) & set(candidate) & set(METRIC_NAMES))
    deltas = {name: round(float(candidate[name]) - float(baseline[name]), 6)
              for name in common}
    regressions = [
        name for name, delta in deltas.items()
        if (name in {"latency_ms", "tokens"} and delta > 0)
        or (name not in {"latency_ms", "tokens"} and delta < 0)
    ]
    blocked = [] if sample_size >= minimum_samples else [
        f"requires at least {minimum_samples} verified samples"
    ]
    return {
        "eligible": not blocked and not regressions,
        "sample_size": sample_size,
        "deltas": deltas,
        "regressions": regressions,
        "blocked_reasons": blocked,
    }
