async def load_positive_examples(pool,limit=20):
    async with pool.acquire() as conn:
        rows=await conn.fetch("SELECT user_question,agent_response,retrieved_docs FROM feedback WHERE rating=1 ORDER BY created_at DESC LIMIT $1",limit)
    return [{"question":r["user_question"] or "","answer":r["agent_response"] or "","contexts":[str(x) for x in (r["retrieved_docs"] or [])],"ground_truth":r["agent_response"] or ""} for r in rows]
