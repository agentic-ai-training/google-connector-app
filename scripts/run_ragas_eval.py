import asyncio
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_recall
from app.db.connection import get_pool,close_pool
from app.mlops.ragas_eval import load_positive_examples
async def main():
    pool=await get_pool(); examples=await load_positive_examples(pool)
    if not examples:
        print("No positive feedback examples available; evaluation skipped")
    else:
        scores=await asyncio.to_thread(evaluate,Dataset.from_list(examples),metrics=[faithfulness,answer_relevancy,context_recall]); print(scores)
    await close_pool()
if __name__=="__main__": asyncio.run(main())
