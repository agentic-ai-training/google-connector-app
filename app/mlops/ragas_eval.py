import json


async def load_positive_examples(pool, limit=20):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id,session_id,prompt_id,user_question,agent_response,retrieved_docs
               FROM feedback WHERE rating=1
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
            "contexts": [str(item) for item in contexts],
            "ground_truth": row["agent_response"] or "",
        })
    return examples
