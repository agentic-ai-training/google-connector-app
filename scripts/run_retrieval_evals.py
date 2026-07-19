#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.connection import close_pool, get_pool
from app.rag.evaluation import aggregate_metrics, retrieval_metrics
from app.rag.retriever import hybrid_retrieve


async def run(path: Path, k: int) -> int:
    cases = json.loads(path.read_text(encoding="utf-8"))
    pool = await get_pool()
    rows = []
    failures = []
    try:
        for case in cases:
            results = await hybrid_retrieve(
                case["query"], pool=pool, user_id=case["user_id"], top_k=k,
                filters=case.get("filters"),
            )
            metrics = retrieval_metrics(
                [str(item["source_id"]) for item in results],
                set(map(str, case["relevant_source_ids"])), k,
            )
            rows.append(metrics)
            if metrics[f"recall@{k}"] < float(case.get("minimum_recall", 1.0)):
                failures.append({"id": case["id"], "metrics": metrics})
    finally:
        await close_pool()
    print(json.dumps({
        "suite": path.name, "cases": len(cases), "aggregate": aggregate_metrics(rows),
        "failures": failures,
    }, indent=2))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    return asyncio.run(run(args.dataset, args.k))


if __name__ == "__main__":
    raise SystemExit(main())
