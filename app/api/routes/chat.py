import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db.connection import get_pool
from app.db.prompt_service import get_prompt, record_metric

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    session_id: str = Field(min_length=1, max_length=200)


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    pool = await get_pool()
    prompt, assignment_id = await get_prompt(
        "supervisor_system", req.session_id, pool=pool
    )
    prompt_id = prompt.get("id") if prompt else None
    system_prompt = prompt.get("content") if prompt else ""

    async def events():
        started = time.perf_counter()
        final = ""
        model_used = ""
        task_complete = False
        error_type = None
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO conversation_history
                   (session_id,user_id,role,content)
                   VALUES($1,$2,'user',$3)""",
                req.session_id,
                request.state.user_id,
                req.message,
            )
        try:
            graph = request.app.state.agent_graph
            initial = {
                "message": req.message,
                "session_id": req.session_id,
                "user_id": request.state.user_id,
                "messages": [],
                "system_prompt": system_prompt,
                "prompt_id": str(prompt_id) if prompt_id else None,
                "assignment_id": str(assignment_id) if assignment_id else None,
            }
            config = {"configurable": {"thread_id": req.session_id}}
            async for update in graph.astream(initial, config=config):
                for node_output in update.values():
                    if not isinstance(node_output, dict):
                        continue
                    model_used = node_output.get("model_to_use", model_used)
                    output = node_output.get("output")
                    if output and output != final:
                        final = output
                    task_complete = node_output.get("task_complete", task_complete)
            for token in final.split(" "):
                yield f"data: {json.dumps({'token': token + ' ', 'done': False})}\n\n"
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO conversation_history
                       (session_id,user_id,role,content,model_used)
                       VALUES($1,$2,'assistant',$3,$4)""",
                    req.session_id,
                    request.state.user_id,
                    final,
                    model_used,
                )
        except Exception as exc:
            error_type = type(exc).__name__
            final = f"I couldn't complete that request: {exc}"
            yield f"data: {json.dumps({'token': final, 'done': False})}\n\n"
        finally:
            total_ms = int((time.perf_counter() - started) * 1000)
            asyncio.create_task(
                record_metric(
                    pool=pool,
                    assignment_id=assignment_id,
                    prompt_id=prompt_id,
                    session_id=req.session_id,
                    total_latency_ms=total_ms,
                    task_completed=task_complete,
                    error_occurred=error_type is not None,
                    error_type=error_type,
                )
            )
        yield f"data: {json.dumps({'token': '', 'done': True, 'session_id': req.session_id})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
