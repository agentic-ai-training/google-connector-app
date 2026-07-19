#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runs.planner import build_plan, validate_plan
from app.evaluation.metrics import evaluate_plan


def main() -> int:
    path = Path(__file__).resolve().parents[1] / "evaluations" / "golden_tasks.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    failures = []
    scores = []
    for case in cases:
        plan, policy = build_plan(case["request"])
        checks = {
            "services": plan.services == case["services"],
            "approval": policy["requires_approval"] == case["approval"],
            "valid_plan": not validate_plan(plan),
        }
        score = evaluate_plan(plan, case)
        scores.append({"id": case["id"], **score})
        checks["plan_quality"] = score["plan_correctness"] == 1.0
        checks["cost"] = score["cost_efficiency"] == 1.0
        if "rag" in case:
            checks["rag"] = policy["rag_mode"] == case["rag"]
        if "clarifications" in case:
            checks["clarifications"] = (
                len(policy["required_clarifications"]) == case["clarifications"]
            )
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            failures.append({"id": case["id"], "failed": failed,
                             "actual_services": plan.services,
                             "policy": policy})
    print(json.dumps({
        "suite": "planner-golden-v1", "cases": len(cases),
        "passed": len(cases) - len(failures), "failures": failures,
        "mean_plan_correctness": round(
            sum(item["plan_correctness"] for item in scores) / max(1, len(scores)), 4
        ),
        "scores": scores,
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
