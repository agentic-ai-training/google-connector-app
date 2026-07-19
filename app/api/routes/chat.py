import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db.connection import get_pool
from app.db.google_clients import request_google_credentials
from app.db.oauth_credentials import load_google_credentials
from app.config.settings import get_settings
from app.db.prompt_service import get_prompt, record_metric
from app.runs.planner import classify_request

router = APIRouter()

CAPABILITIES = (
    "I can work with Gmail, Google Calendar, Drive, Docs, Sheets, Tasks, "
    "Google Chat, Contacts, and Google Meet. For Meet I can create an instant "
    "meeting link, inspect a meeting space, list recent conference records, and "
    "list conference participants. I can also schedule Calendar events with a "
    "Google Meet link. Tell me the action and any required names, dates, or IDs."
)


def capability_answer(message: str) -> str | None:
    text = " ".join(message.lower().split())
    capability_phrases = (
        "what can you do", "can you only", "what about google meet",
        "what about meet", "other operations", "which operations",
        "what operations", "your capabilities",
    )
    if any(phrase in text for phrase in capability_phrases):
        return CAPABILITIES
    return None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    session_id: str = Field(min_length=1, max_length=200)


def classify_graph_results(node_output: dict) -> tuple[list | None, list | None]:
    """Separate RAG documents from results returned by executed tools."""
    results = node_output.get("tool_results")
    if not isinstance(results, list):
        return None, None
    if "retrieved_context" in node_output:
        return results, None
    return None, results


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    settings = get_settings()
    if not settings.legacy_chat_enabled:
        raise HTTPException(410, "Legacy chat is disabled; use the durable runs API")
    policy = classify_request(req.message)
    if policy["requires_approval"]:
        raise HTTPException(
            409,
            "This request contains a high-risk external write. Use the durable run flow to review and approve it.",
        )
    pool = await get_pool()
    google_credentials = await load_google_credentials(pool, request.state.user_id)
    if google_credentials is None and not settings.allow_dev_auth:
        raise HTTPException(403, "Connect your Google account before chatting")
    direct_answer = capability_answer(req.message)
    prompt, assignment_id = await get_prompt(
        "supervisor_system", req.session_id, pool=pool
    )
    prompt_id = prompt.get("id") if prompt else None
    system_prompt = prompt.get("content") if prompt else ""

    async def events():
        started = time.perf_counter()
        final = ""
        model_used = ""
        retrieved_docs = []
        tool_results = []
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
            if direct_answer is not None:
                final = direct_answer
                model_used = "deterministic-capabilities"
                task_complete = True
                for token in final.split(" "):
                    yield f"data: {json.dumps({'token': token + ' ', 'done': False})}\n\n"
                async with pool.acquire() as conn:
                    await conn.execute(
                        """INSERT INTO conversation_history
                           (session_id,user_id,role,content,tool_calls,tool_results,
                            model_used)
                           VALUES($1,$2,'assistant',$3,'[]'::jsonb,'[]'::jsonb,$4)""",
                        req.session_id, request.state.user_id, final, model_used,
                    )
                yield f"data: {json.dumps({'token': '', 'done': True, 'session_id': req.session_id})}\n\n"
                return
            credential_token = request_google_credentials.set(google_credentials)
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
                for _node_name, node_output in update.items():
                    if not isinstance(node_output, dict):
                        continue
                    retrieved, executed = classify_graph_results(node_output)
                    if retrieved is not None:
                        retrieved_docs = retrieved
                    if executed is not None:
                        tool_results.extend(executed)
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
                       (session_id,user_id,role,content,tool_calls,tool_results,
                        model_used)
                       VALUES($1,$2,'assistant',$3,$4::jsonb,$5::jsonb,$6)""",
                    req.session_id,
                    request.state.user_id,
                    final,
                    json.dumps(tool_results, default=str),
                    json.dumps(retrieved_docs, default=str),
                    model_used,
                )
        except Exception as exc:
            error_type = type(exc).__name__
            final = f"I couldn't complete that request: {exc}"
            yield f"data: {json.dumps({'token': final, 'done': False})}\n\n"
        finally:
            if "credential_token" in locals():
                request_google_credentials.reset(credential_token)
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
