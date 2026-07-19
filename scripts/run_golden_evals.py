#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runs.planner import build_plan, validate_plan


def main() -> int:
    path = Path(__file__).resolve().parents[1] / "evaluations" / "golden_tasks.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    failures = []
    for case in cases:
        plan, policy = build_plan(case["request"])
        checks = {
            "services": plan.services == case["services"],
            "approval": policy["requires_approval"] == case["approval"],
            "valid_plan": not validate_plan(plan),
        }
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
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
