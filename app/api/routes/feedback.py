import json
import re
import hashlib

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.connection import get_pool
from app.evaluation.collector import record_run_evaluation

router = APIRouter()
ALLOWED_CATEGORIES = {
    "wrong_step", "missing_result", "too_slow", "wrong_tool", "wrong_data",
    "unsafe_action", "incorrect_artifact", "other",
}


class FeedbackRequest(BaseModel):
    session_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None
    rating: int
    categories: list[str] = Field(default_factory=list, max_length=8)
    comment: str | None = Field(default=None, max_length=4_000)
    expected_result: str | None = Field(default=None, max_length=8_000)
    consented_for_learning: bool = False


def _sanitize(value: str | None) -> str:
    text = value or ""
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email-redacted]", text)
    text = re.sub(r"(?i)(token|secret|authorization|api[_ -]?key)\s*[:=]\s*\S+",
                  r"\1=[redacted]", text)
    return text[:8_000]


def _sanitize_value(value):
    """Recursively sanitize trajectory payloads before marking them sanitized."""
    if isinstance(value, str):
        return _sanitize(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


def _dataset_split(user_id: str) -> str:
    """Keep every trajectory from one user in one stable split to prevent leakage."""
    bucket = int(hashlib.sha256(user_id.lower().encode()).hexdigest()[:8], 16) % 100
    if bucket < 80:
        return "train"
    if bucket < 90:
        return "validation"
    return "test"


@router.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    if req.rating not in (-1, 1):
        raise HTTPException(422, "rating must be +1 or -1")
    if not req.run_id and not req.session_id:
        raise HTTPException(422, "run_id or session_id is required")
    unknown = set(req.categories) - ALLOWED_CATEGORIES
    if unknown:
        raise HTTPException(422, f"unknown feedback categories: {sorted(unknown)}")
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if req.run_id:
            run = await conn.fetchrow(
                "SELECT * FROM agent_runs WHERE id=$1 AND user_id=$2",
                req.run_id, request.state.user_id,
            )
            if not run:
                raise HTTPException(404, "Run not found")
            session_id = run["session_id"]
            question = run["request"]
            response = (run["result"] or {}).get("output", "")
            retrieved_docs = []
        else:
            session_id = req.session_id
            assistant = await conn.fetchrow(
                """SELECT content,created_at,tool_results FROM conversation_history
                   WHERE session_id=$1 AND user_id=$2 AND role='assistant'
                   ORDER BY created_at DESC LIMIT 1""",
                session_id, request.state.user_id,
            )
            if not assistant:
                raise HTTPException(404, "No assistant response found for session")
            question = await conn.fetchval(
                """SELECT content FROM conversation_history
                   WHERE session_id=$1 AND user_id=$2 AND role='user' AND created_at <= $3
                   ORDER BY created_at DESC LIMIT 1""",
                session_id, request.state.user_id, assistant["created_at"],
            )
            response = assistant["content"]
            retrieved_docs = assistant["tool_results"] or []
            if isinstance(retrieved_docs, str):
                retrieved_docs = json.loads(retrieved_docs)
        await conn.execute(
            """INSERT INTO feedback
               (session_id,user_id,user_question,agent_response,retrieved_docs,rating,
                comment,run_id,step_id,categories,consented_for_learning,sanitized,
                expected_result)
               VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,$11,$12,$13)""",
            session_id, request.state.user_id, question, response,
            json.dumps(retrieved_docs, default=str), req.rating, req.comment,
            req.run_id, req.step_id, req.categories, req.consented_for_learning,
            req.consented_for_learning, req.expected_result,
        )
        if req.run_id and req.consented_for_learning:
            steps = [dict(row) for row in await conn.fetch(
                """SELECT step_key,sequence_no,service,operation,read_only,status,
                          error_category,duration_ms,input_tokens,output_tokens
                   FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no""",
                req.run_id,
            )]
            attempts = [dict(row) for row in await conn.fetch(
                """SELECT tool_name,attempt_no,status,duration_ms,error_category
                   FROM agent_tool_attempts WHERE run_id=$1 ORDER BY created_at""",
                req.run_id,
            )]
            await conn.execute(
                """INSERT INTO learning_trajectories
                   (run_id,consented,sanitized,state,decision,action,observation,reward,
                    next_state,dataset_split)
                   VALUES($1,TRUE,TRUE,$2::jsonb,$3::jsonb,$4::jsonb,$5::jsonb,
                          $6::jsonb,$7::jsonb,$8)""",
                req.run_id,
                json.dumps({"request": _sanitize(question)}),
                json.dumps({"plan": _sanitize_value(run["plan"])}, default=str),
                json.dumps({"steps": steps, "tool_attempts": attempts}, default=str),
                json.dumps({"response": _sanitize(response), "incident":
                            _sanitize_value(run["incident_summary"]),
                            "comment": _sanitize(req.comment)}, default=str),
                json.dumps({"rating": req.rating, "categories": req.categories}),
                json.dumps({"expected_result": _sanitize(req.expected_result)}),
                _dataset_split(request.state.user_id),
            )
    if req.run_id:
        await record_run_evaluation(pool, req.run_id)
    return {"status": "recorded", "learning_candidate": req.consented_for_learning}
