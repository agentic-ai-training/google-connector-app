"""Groq-only, no-execution candidate generation with durable checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from groq import APIStatusError, AsyncGroq, RateLimitError

from app.config.settings import get_settings
from app.improvements.candidates import (
    ALLOWED_ROOTS, candidate_digest, file_digest, infer_candidate_kind,
    validate_candidate_files,
)
from app.improvements.builder_tools import BoundedRepositoryTools

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
MODEL_POLICY_VERSION = "adaptive-roles-v1"
TOOL_POLICY_VERSION = "bounded-repo-tools-v1"
BUILDER_HISTORY_MAX_CHARS = 24_000
BUILDER_413_RETRY_MAX_CHARS = 12_000


def candidate_model_order(job: dict) -> list[str]:
    """Return the builder-only model chain without changing runtime routing."""
    configured = get_settings().candidate_builder_fallback_models.split(",")
    ordered = [str(job["model_name"]), *(item.strip() for item in configured)]
    return list(dict.fromkeys(model for model in ordered if model))


async def _candidate_completion(client: AsyncGroq, job: dict, **kwargs):
    """Use another Groq quality model only when candidate generation is limited."""
    last_error: RateLimitError | None = None
    for model in candidate_model_order(job):
        for attempt in range(2):
            try:
                response = await client.chat.completions.create(model=model, **kwargs)
                return response, model
            except RateLimitError as exc:
                last_error = exc
                response = getattr(exc, "response", None)
                raw = response.headers.get("retry-after") if response is not None else None
                try:
                    retry_after = float(raw) if raw else 0.0
                except ValueError:
                    retry_after = 0.0
                # A short window is normally TPM; wait once. Long waits are TPD and
                # should advance immediately to the next builder-only quality model.
                if attempt == 0 and 0 < retry_after <= 30:
                    await asyncio.sleep(retry_after)
                    continue
                break
            except APIStatusError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 413 and attempt == 0 and kwargs.get("messages"):
                    kwargs = dict(kwargs)
                    kwargs["messages"] = _fit_builder_history(
                        kwargs["messages"], max_chars=BUILDER_413_RETRY_MAX_CHARS,
                    )
                    kwargs["max_tokens"] = min(int(kwargs.get("max_tokens") or 2048), 2048)
                    continue
                raise
    assert last_error is not None
    raise last_error


def _compact_builder_tool_call(call: dict) -> dict:
    """Remove staged file bodies from history after the in-memory tool consumed them."""
    compacted = json.loads(json.dumps(call))
    function = compacted.get("function") or {}
    if function.get("name") == "stage_candidate_file":
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        content = str(arguments.get("content") or "")
        if content:
            digest = hashlib.sha256(content.encode()).hexdigest()
            arguments["content"] = (
                f"[staged in memory; {len(content)} chars; sha256:{digest}; body omitted]"
            )
            function["arguments"] = json.dumps(arguments, sort_keys=True)
    return compacted


def _fit_builder_history(
    messages: list[dict], *, max_chars: int = BUILDER_HISTORY_MAX_CHARS,
) -> list[dict]:
    """Bound cumulative tool history while preserving tool-call/result relationships."""
    fitted = json.loads(json.dumps(messages))

    def size() -> int:
        return len(json.dumps(fitted, default=str))

    if size() <= max_chars:
        return fitted
    for message in fitted:
        if message.get("role") != "tool":
            continue
        message["content"] = json.dumps({
            "compacted": True,
            "reason": "earlier builder tool result removed to preserve request budget",
        })
        if size() <= max_chars:
            return fitted
    if size() > max_chars:
        raise RuntimeError("Candidate builder history exceeded its bounded request budget")
    return fitted


def candidate_review_projection(candidate: dict | None) -> dict:
    """Describe a candidate without copying complete generated files into a prompt."""
    value = candidate or {}
    files = []
    for item in value.get("files") or []:
        content = item.get("content") or ""
        files.append({
            "path": item.get("path"),
            "change_type": item.get("change_type"),
            "content_chars": len(content),
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "preview": content[:500],
        })
    return {
        "files": files,
        "exact_diff_preview": str(value.get("exact_diff") or "")[:8_000],
        "rollback_plan": value.get("rollback_plan"),
        "validation_commands": value.get("validation_commands") or [],
        "full_content_access": "use read_staged_candidate_file",
    }


def normalize_candidate_contract(candidate: dict) -> dict:
    """Coerce harmless model shape variance; never invent validation success."""
    value = dict(candidate or {})
    rollback = value.get("rollback_plan")
    if isinstance(rollback, str):
        value["rollback_plan"] = {"action": rollback, "automatic": False}
    elif not isinstance(rollback, dict):
        value["rollback_plan"] = {
            "action": "route traffic to the frozen base version", "automatic": True,
        }
    commands = value.get("validation_commands")
    if isinstance(commands, str):
        value["validation_commands"] = [commands]
    elif not isinstance(commands, list):
        value["validation_commands"] = []
    return value

def choose_builder_mode(risk_level: str, change_scope: list[str]) -> str:
    return "multi_role" if risk_level in {"high", "critical"} or len(change_scope) > 3 else "single"

async def enqueue_candidate_build(pool, proposal_id, incident: dict, option: dict, actor: str):
    settings = get_settings()
    if not settings.candidate_builder_enabled:
        return None
    scope = option.get("change_scope") or []
    mode = choose_builder_mode(incident.get("risk_level", "medium"), scope)
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO candidate_builds
               (proposal_id,selected_option,mode,base_commit,model_name,
                model_policy_version,tool_policy_version,token_budget,sanitized_input,
                created_by)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10) RETURNING id""",
            proposal_id, option["id"], mode, get_settings().deployment_version,
            settings.candidate_builder_model, MODEL_POLICY_VERSION, TOOL_POLICY_VERSION,
            settings.candidate_builder_job_token_budget,
            json.dumps({
                "incident_id": str(incident["id"]), "title": incident["title"],
                "stage": incident["stage"], "category": incident["category"],
                "component": incident["component"], "service": incident["service"],
                "operation": incident["operation"], "root_cause": incident["root_cause"],
                "request_shape": incident.get("request_shape") or {},
                "selected_option": option, "contains_raw_user_content": False,
            }), actor,
        )


def _candidate_prompt(job: dict, sources: list[dict], role: str) -> str:
    reviewing = role == "independent_safety_reviewer"
    return json.dumps({
        "role": role,
        "objective": (
            "Independently reject or revise the supplied candidate. Return JSON only with "
            "approved, reason, and optional revised_candidate."
            if reviewing else
            "Inspect through the bounded repository tools, stage a minimal implementation and "
            "regression tests, then return JSON only with files[{path,change_type,content}], "
            "exact_diff, rollback_plan, validation_commands, notes."
        ),
        "rules": [
            "Use only supplied repository paths or create files under app/, tests/, knowledge/, config/, docs/.",
            "Never include secrets, credentials, raw user content, shell commands, network calls, or production mutations.",
            "Do not claim tests passed. CI validates the frozen candidate separately.",
            "Preserve unrelated behavior and include a no-network regression for the reported failure.",
            "Tool calls can only read or stage in memory; they cannot execute, publish, or authorize changes.",
            "For a new tool include schema, least scopes, adapter, registry, projection, verifier, tests, and draft OKF.",
        ],
        "incident": job["sanitized_input"], "sources": sources,
    }, default=str)


async def _groq_json(
    job: dict, sources: list[dict], role: str,
) -> tuple[dict, int, list[str]]:
    settings = get_settings()
    client = AsyncGroq(api_key=settings.groq_api_key)
    response, model = await _candidate_completion(
        client, job,
        messages=[{"role": "user", "content": _candidate_prompt(job, sources, role)}],
        temperature=0.1,
        max_tokens=min(settings.candidate_builder_max_output_tokens, job["token_budget"]),
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    usage = response.usage
    return (
        data,
        int((usage.prompt_tokens or 0) + (usage.completion_tokens or 0)),
        [model],
    )


async def _groq_tool_json(
    job: dict, tools: BoundedRepositoryTools, role: str,
    prior_candidate: dict | None = None,
) -> tuple[dict, int, list[str]]:
    """Run a bounded tool loop; Groq never receives a shell or network tool."""
    settings = get_settings()
    client = AsyncGroq(api_key=settings.groq_api_key)
    messages = [{
        "role": "user",
        "content": _candidate_prompt(
            job,
            ([{"candidate_for_revision": candidate_review_projection(prior_candidate)}]
             if prior_candidate else [{
                "repository": "ephemeral checkout",
                "approved_roots": list(ALLOWED_ROOTS),
                "read_limit_bytes": tools.max_read_bytes,
                "tool_call_limit": tools.max_calls,
                "changed_file_limit": tools.max_files,
            }]),
            role,
        ),
    }]
    tokens = 0
    models_used: list[str] = []
    for _ in range(12):
        if tokens >= job["token_budget"]:
            raise RuntimeError("Candidate token budget exhausted during tool reasoning")
        remaining = max(256, job["token_budget"] - tokens)
        messages = _fit_builder_history(messages)
        response, model = await _candidate_completion(
            client, job, messages=messages,
            tools=tools.schemas(), tool_choice="auto", temperature=0.1,
            max_tokens=min(settings.candidate_builder_max_output_tokens, remaining),
        )
        if model not in models_used:
            models_used.append(model)
        usage = response.usage
        tokens += int((usage.prompt_tokens or 0) + (usage.completion_tokens or 0))
        message = response.choices[0].message
        if message.tool_calls:
            calls = [{
                "id": call.id, "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            } for call in message.tool_calls]
            messages.append({
                "role": "assistant", "content": message.content or "",
                "tool_calls": [_compact_builder_tool_call(call) for call in calls],
            })
            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                    result = tools.execute(call.function.name, arguments)
                except Exception as exc:
                    result = {"error": type(exc).__name__, "detail": str(exc)[:500]}
                projected = tools.project_result(call.function.name, result)
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "name": call.function.name,
                    "content": json.dumps(projected, default=str),
                })
            continue
        try:
            candidate = json.loads(message.content or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Groq candidate output was not valid JSON") from exc
        if tools.staged_files():
            candidate["files"] = tools.staged_files()
            candidate.setdefault("exact_diff", tools.diff()["diff"])
        return candidate, tokens, models_used
    raise RuntimeError("Candidate builder exceeded its bounded reasoning/tool rounds")


async def generate_candidate_draft(
    job: dict,
) -> tuple[dict, int, list[str], list[str]]:
    """Generate a patch from sanitized facts and a bounded checkout only."""
    sanitized = dict(job["sanitized_input"] or {})
    scope = sanitized.get("selected_option", {}).get("change_scope", [])
    tool_extension = any(
        "tool" in value.casefold() for value in [sanitized.get("component", ""), *scope]
    )
    first_role = "tool_extension_designer" if tool_extension else (
        "coordinator" if job["mode"] == "single" else "investigator_and_patch_author"
    )
    roles = [first_role] + (
        ["independent_safety_reviewer"] if job["mode"] == "multi_role" else []
    )
    repository_tools = BoundedRepositoryTools(ROOT)
    candidate = None
    tokens = 0
    models_used: list[str] = []
    for role in roles:
        if tokens >= job["token_budget"]:
            raise RuntimeError("Candidate token budget exhausted before review")
        if role == "independent_safety_reviewer":
            output, used, call_models = await _groq_tool_json(
                job, repository_tools, role, candidate,
            )
        else:
            output, used, call_models = await _groq_tool_json(
                job, repository_tools, role, candidate,
            )
        tokens += used
        for model in call_models:
            if model not in models_used:
                models_used.append(model)
        if role == "independent_safety_reviewer":
            if output.get("approved") is False:
                raise RuntimeError(
                    output.get("reason") or "Independent review rejected candidate"
                )
            candidate = output.get("revised_candidate") or candidate
            candidate["files"] = repository_tools.staged_files()
            candidate["exact_diff"] = repository_tools.diff()["diff"]
        else:
            candidate = output
    candidate = normalize_candidate_contract(candidate or {})
    candidate.setdefault("exact_diff", "generated files are the authoritative candidate")
    candidate.setdefault("rollback_plan", {"action": "route traffic to base version"})
    candidate.setdefault("validation_commands", [])
    return candidate, tokens, roles, models_used


async def store_candidate_draft(
    pool, build_id, candidate: dict, tokens: int, roles: list[str],
    models_used: list[str] | None = None,
) -> dict:
    """Freeze a generated draft; execution and pass/fail claims remain CI-only."""
    files = candidate.get("files") or []
    errors = validate_candidate_files(files)
    if errors:
        raise ValueError("; ".join(errors))
    async with pool.acquire() as conn, conn.transaction():
        job = await conn.fetchrow(
            "SELECT * FROM candidate_builds WHERE id=$1 FOR UPDATE", build_id,
        )
        if not job or job["status"] not in {"queued", "investigating"}:
            raise ValueError("Candidate build is unavailable or already finalized")
        exact_diff = candidate["exact_diff"]
        rollback = candidate["rollback_plan"]
        validation = {
            "passed": False, "status": "awaiting_trusted_ci",
            "commands": candidate.get("validation_commands") or [],
            "builder_did_not_execute_code": True,
        }
        candidate_kind = infer_candidate_kind(files)
        digest = candidate_digest(
            job["base_commit"], files, validation,
            candidate_kind=candidate_kind, candidate_version=f"build-{job['id']}",
            exact_diff=exact_diff, rollback_plan=rollback,
        )
        await conn.execute(
            "DELETE FROM improvement_candidate_files WHERE proposal_id=$1",
            job["proposal_id"],
        )
        for item in files:
            preimage = (
                (ROOT / item["path"]).read_text()
                if (ROOT / item["path"]).is_file() else None
            )
            await conn.execute(
                """INSERT INTO candidate_build_files
                   (build_id,path,change_type,preimage_hash,result_hash,content)
                   VALUES($1,$2,$3,$4,$5,$6)""",
                job["id"], item["path"], item["change_type"],
                file_digest(preimage) if preimage is not None else None,
                file_digest(item.get("content")), item.get("content"),
            )
            await conn.execute(
                """INSERT INTO improvement_candidate_files
                   (proposal_id,path,change_type,content,content_hash)
                   VALUES($1,$2,$3,$4,$5)""",
                job["proposal_id"], item["path"], item["change_type"],
                item.get("content"), file_digest(item.get("content")),
            )
        await conn.execute(
            """UPDATE candidate_builds SET status='drafted',tokens_used=$1,
               canonical_digest=$2,checkpoint=$3::jsonb,updated_at=now() WHERE id=$4""",
            tokens, digest, json.dumps({
                "roles_completed": roles,
                "models_used": models_used or [job["model_name"]],
            }), job["id"],
        )
        await conn.execute(
            """UPDATE improvement_proposals SET candidate_kind=$1,
               candidate_state='implementation_draft',candidate_version=$2,
               exact_diff=$3,rollback_plan=$4::jsonb,validation_report=$5::jsonb,
               candidate_manifest=$6::jsonb,content_hash=$7,updated_at=now()
               WHERE id=$8""",
            candidate_kind, f"build-{job['id']}", exact_diff, json.dumps(rollback),
            json.dumps(validation), json.dumps({
                "build_id": str(job["id"]), "mode": job["mode"],
                "model": job["model_name"],
                "models_used": models_used or [job["model_name"]],
                "tool_policy": TOOL_POLICY_VERSION,
                "applicability": {
                    "services": [job["sanitized_input"].get("service")]
                    if job["sanitized_input"].get("service") else [],
                    "operations": [job["sanitized_input"].get("operation")]
                    if job["sanitized_input"].get("operation") else [],
                    "rag_modes": [
                        (job["sanitized_input"].get("request_shape") or {}).get(
                            "rag_mode", "none"
                        )
                    ],
                },
                "canary_eligible": False,
            }), digest, job["proposal_id"],
        )
    return {"build_id": str(build_id), "status": "drafted", "content_hash": digest}


async def process_one_candidate_build(pool) -> bool:
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """SELECT b.*,p.proposal_key,p.risk_level FROM candidate_builds b
               JOIN improvement_proposals p ON p.id=b.proposal_id
               WHERE b.status='queued' ORDER BY b.created_at
               FOR UPDATE SKIP LOCKED LIMIT 1"""
        )
        if not row:
            return False
        await conn.execute(
            "UPDATE candidate_builds SET status='investigating',updated_at=now() WHERE id=$1",
            row["id"],
        )
    job = dict(row)
    try:
        candidate, tokens, roles, models_used = await asyncio.wait_for(
            generate_candidate_draft(job),
            timeout=get_settings().candidate_builder_timeout_seconds,
        )
        await store_candidate_draft(
            pool, job["id"], candidate, tokens, roles, models_used,
        )
        return True
    except Exception as exc:
        logger.exception("Candidate build %s failed", job["id"])
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE candidate_builds SET status='failed',error_message=$1,
                   updated_at=now(),completed_at=now() WHERE id=$2""",
                str(exc)[:2000], job["id"],
            )
            proposal = await conn.fetchrow(
                "SELECT * FROM improvement_proposals WHERE id=$1", job["proposal_id"],
            )
            if proposal:
                from app.improvements.failure_intelligence import release_theme_for_proposal
                await release_theme_for_proposal(conn, dict(proposal))
        return True


async def candidate_builder_loop(pool, stop_event: asyncio.Event):
    while not stop_event.is_set():
        worked = await process_one_candidate_build(pool)
        if worked:
            continue
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=get_settings().candidate_builder_poll_seconds,
            )
        except asyncio.TimeoutError:
            pass
