import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datasets import Dataset
from ragas import evaluate
from ragas.metrics.collections import answer_relevancy, context_recall, faithfulness

from app.db.connection import close_pool, get_pool
from app.db.prompt_service import record_metric
from app.mlops.ragas_eval import load_positive_examples


async def main():
    pool = await get_pool()
    try:
        examples = await load_positive_examples(pool)
        if not examples:
            print("No positive feedback examples available; evaluation skipped")
            return
        dataset_rows = [
            {key: value for key, value in row.items()
             if key in {"question", "answer", "contexts", "ground_truth"}}
            for row in examples
        ]
        scores = await asyncio.to_thread(
            evaluate,
            Dataset.from_list(dataset_rows),
            metrics=[faithfulness, answer_relevancy, context_recall],
        )
        frame = scores.to_pandas()
        for index, example in enumerate(examples):
            row = frame.iloc[index]
            await record_metric(
                pool=pool,
                session_id=example["session_id"],
                prompt_id=example["prompt_id"],
                faithfulness=float(row.get("faithfulness", 0)),
                answer_relevancy=float(row.get("answer_relevancy", 0)),
                context_recall=float(row.get("context_recall", 0)),
            )
        print(scores)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
