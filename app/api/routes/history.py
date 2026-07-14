from fastapi import APIRouter
from app.db.connection import get_pool
router=APIRouter()
@router.get("/history/{session_id}")
async def history(session_id:str):
    pool=await get_pool()
    async with pool.acquire() as conn:
        rows=await conn.fetch("SELECT role,content,created_at FROM (SELECT role,content,created_at FROM conversation_history WHERE session_id=$1 ORDER BY created_at DESC LIMIT 50) x ORDER BY created_at",session_id)
    return {"messages":[dict(r) for r in rows]}
