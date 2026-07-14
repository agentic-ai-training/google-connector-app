import json, time
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.agents.supervisor import agent_graph
from app.db.connection import get_pool
router=APIRouter()
class ChatRequest(BaseModel): message:str; session_id:str
@router.post("/chat")
async def chat(req:ChatRequest,request:Request):
    async def events():
        pool=await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO conversation_history(session_id,user_id,role,content) VALUES($1,$2,'user',$3)",req.session_id,request.state.user_id,req.message)
        final=""
        try:
            result=await agent_graph.ainvoke({"message":req.message,"session_id":req.session_id,"user_id":request.state.user_id,"messages":[]})
            final=result.get("output","")
            for token in final.split(" "):
                yield f"data: {json.dumps({'token':token+' ','done':False})}\n\n"
            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO conversation_history(session_id,user_id,role,content,model_used) VALUES($1,$2,'assistant',$3,$4)",req.session_id,request.state.user_id,final,result.get("model_to_use"))
        except Exception as exc:
            yield f"data: {json.dumps({'token':str(exc),'done':False})}\n\n"
        yield f"data: {json.dumps({'token':'','done':True,'session_id':req.session_id})}\n\n"
    return StreamingResponse(events(),media_type="text/event-stream")
