import json


def _context_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("content") or item.get("text") or item)
    return str(item)


async def load_evaluation_examples(pool, limit=20):
    """Use corrected negatives when available; never call a bad answer ground truth."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id,session_id,prompt_id,user_question,agent_response,
                      retrieved_docs,rating,expected_result
               FROM feedback
               WHERE rating=1 OR (rating=-1 AND expected_result IS NOT NULL
                                  AND length(trim(expected_result))>0)
               ORDER BY created_at DESC LIMIT $1""",
            limit,
        )
    examples = []
    for row in rows:
        contexts = row["retrieved_docs"] or []
        if isinstance(contexts, str):
            contexts = json.loads(contexts)
        examples.append({
            "feedback_id": str(row["id"]),
            "session_id": row["session_id"],
            "prompt_id": str(row["prompt_id"]) if row["prompt_id"] else None,
            "question": row["user_question"] or "",
            "answer": row["agent_response"] or "",
            "contexts": [_context_text(item) for item in contexts],
            "ground_truth": row["expected_result"] or row["agent_response"] or "",
            "rating": row["rating"],
        })
    return examples


load_positive_examples = load_evaluation_examples
