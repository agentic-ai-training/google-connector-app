#!/usr/bin/env python3
"""Offline, no-network policy comparison over the versioned golden set."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.metrics import compare_policy_metrics, evaluate_plan
from app.runs.planner import build_plan


def main() -> int:
    path = Path(__file__).parents[1] / "evaluations" / "golden_tasks.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    scores = []
    for case in cases:
        plan, _ = build_plan(case["request"])
        scores.append(evaluate_plan(plan, case)["plan_correctness"])
    candidate = {"plan_correctness": sum(scores) / max(1, len(scores))}
    baseline = {"plan_correctness": 1.0}
    report = compare_policy_metrics(
        baseline, candidate, sample_size=len(cases), minimum_samples=30
    )
    print(json.dumps({
        "suite": "offline-policy-v1", "baseline": baseline,
        "candidate": candidate, **report,
    }, indent=2))
    # Insufficient evidence is a safe non-promotion result, not a broken suite.
    return 1 if report["regressions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
