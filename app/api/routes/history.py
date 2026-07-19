from fastapi import APIRouter, Request

from app.db.connection import get_pool

router = APIRouter()


@router.get("/history/{session_id}")
async def history(session_id: str, request: Request):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT role,content,created_at FROM (
                 SELECT role,content,created_at FROM conversation_history
                 WHERE session_id=$1 AND user_id=$2
                 ORDER BY created_at DESC LIMIT 50
               ) history ORDER BY created_at""",
            session_id, request.state.user_id,
        )
    return {"messages": [dict(row) for row in rows]}
