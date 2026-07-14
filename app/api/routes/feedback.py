from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from app.db.connection import get_pool
router=APIRouter()
class FeedbackRequest(BaseModel):
    session_id:str
    rating:int
@router.post("/feedback")
async def feedback(req:FeedbackRequest, request:Request):
    if req.rating not in (-1,1): raise HTTPException(422,"rating must be +1 or -1")
    pool=await get_pool()
    async with pool.acquire() as conn:
        turn=await conn.fetchrow("SELECT content FROM conversation_history WHERE session_id=$1 AND role='assistant' ORDER BY created_at DESC LIMIT 1",req.session_id)
        await conn.execute("INSERT INTO feedback(session_id,user_id,agent_response,rating) VALUES($1,$2,$3,$4)",req.session_id,request.state.user_id,turn["content"] if turn else None,req.rating)
    return {"status":"recorded"}
