#!/usr/bin/env python3
"""Run no-network Google mutation workflow fixtures."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evaluation.replay import replay_case


def main() -> int:
    path = Path(__file__).parents[1] / "evaluations" / "workflow_replays.json"
    cases = json.loads(path.read_text())
    results = [replay_case(case) for case in cases]
    for result in results:
        print(json.dumps(result.model_dump(), sort_keys=True))
    passed = sum(result.functional_completion == 100 for result in results)
    print(f"workflow replay: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
