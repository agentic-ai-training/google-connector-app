"""Groq-only, no-execution candidate generation with durable checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from collections.abc import Awaitable, Callable

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
TOOL_POLICY_VERSION = "bounded-repo-tools-v5-durable-phases"
BUILDER_HISTORY_MAX_CHARS = 24_000
BUILDER_413_RETRY_MAX_CHARS = 12_000
BUILDER_AUTHOR_MAX_ROUNDS = 8
BUILDER_REVIEWER_MAX_ROUNDS = 5
BUILDER_TOOL_TURN_MAX_TOKENS = 2_048
BUILDER_FINAL_TURN_MAX_TOKENS = 4_096
BUILDER_QUOTA_RETRY_TOKEN_STEPS = (1_024, 512, 256)


def candidate_model_order(job: dict) -> list[str]:
    """Return the builder-only model chain without changing runtime routing."""
    configured = get_settings().candidate_builder_fallback_models.split(",")
    ordered = [str(job["model_name"]), *(item.strip() for item in configured)]
    return list(dict.fromkeys(model for model in ordered if model))


def effective_builder_token_budget(job: dict) -> int:
    """Allow bounded multi-role fallback work without changing runtime LLM budgets."""
    settings = get_settings()
    stored = int(job["token_budget"])
    if len(candidate_model_order(job)) > 1:
        return max(stored, settings.candidate_builder_max_effective_token_budget)
    return stored


def is_tool_generation_failure(exc: Exception) -> bool:
    """Detect Groq's safe failure shape without retaining attempted arguments."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return False
    error = body.get("error") if isinstance(body.get("error"), dict) else body
    return isinstance(error, dict) and "failed_generation" in error


def groq_bad_request_code(exc: Exception) -> str:
    """Classify a Groq 400 without returning prompts or generated arguments."""
    body = getattr(exc, "body", None)
    try:
        value = json.dumps(body or {}, default=str).casefold()
    except (TypeError, ValueError):
        value = ""
    if any(marker in value for marker in (
        "reduce the length", "context_length", "context length", "too many tokens",
        "maximum context", "max_tokens", "message too large",
    )):
        return "model_context_length"
    if "failed_generation" in value:
        return "tool_generation_failed"
    if any(marker in value for marker in (
        "response_format", "tool_choice", "parallel_tool_calls",
        "disable_tool_validation", "unsupported", "not supported",
    )):
        return "model_request_schema_rejected"
    return "model_bad_request"


def _json_tool_protocol_kwargs(kwargs: dict) -> dict:
    """Replace provider-native tools with a locally validated JSON action protocol."""
    value = dict(kwargs)
    schemas = value.pop("tools", [])
    value.pop("tool_choice", None)
    value.pop("parallel_tool_calls", None)
    value.pop("disable_tool_validation", None)
    converted = []
    for message in value.get("messages") or []:
        role = message.get("role")
        if role == "tool":
            converted.append({
                "role": "user",
                "content": json.dumps({
                    "tool_result": {
                        "name": message.get("name"),
                        "call_id": message.get("tool_call_id"),
                        "result": message.get("content"),
                    },
                }),
            })
        elif message.get("tool_calls"):
            converted.append({
                "role": "assistant",
                "content": json.dumps({"tool_calls": message["tool_calls"]}),
            })
        else:
            converted.append({
                "role": role,
                "content": message.get("content") or "",
            })
    converted.insert(0, {
        "role": "system",
        "content": json.dumps({
            "repository_action_protocol": {
                "instruction": (
                    "Return one JSON object. To use a repository tool return "
                    "{\"tool_call\":{\"name\":<allowed name>,\"arguments\":{...}}}. "
                    "When finished return the candidate contract requested by the user."
                ),
                "allowed_tools": schemas,
                "one_tool_per_turn": True,
                "authority": "All names and arguments are validated and executed locally.",
            },
        }),
    })
    value["messages"] = _fit_builder_history(converted)
    value["response_format"] = {"type": "json_object"}
    return value


async def _candidate_completion(
    client: AsyncGroq, job: dict, *, json_tool_protocol: bool = False, **kwargs,
):
    """Use another Groq quality model only when candidate generation is limited."""
    last_error: APIStatusError | None = None
    protocol_mode = json_tool_protocol
    if protocol_mode:
        kwargs = _json_tool_protocol_kwargs(kwargs)
    for model in candidate_model_order(job):
        request_kwargs = dict(kwargs)
        if model == "qwen/qwen3.6-27b":
            request_kwargs["temperature"] = 0.6
            request_kwargs["reasoning_format"] = "hidden"
        if model.startswith("openai/gpt-oss-") and request_kwargs.get("tools"):
            # Groq rejects response_format alongside local tools for GPT-OSS.
            # Tool turns remain provider-validated; JSON mode is restored once
            # tools close for final candidate serialization.
            request_kwargs.pop("response_format", None)
        retried_short_limit = False
        quota_retry_index = 0
        retried_oversize = False
        retried_tool_generation = False
        retried_json_generation = False
        for _ in range(4):
            try:
                response = await client.chat.completions.create(
                    model=model, **request_kwargs,
                )
                return response, model, protocol_mode
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
                if not retried_short_limit and 0 < retry_after <= 30:
                    retried_short_limit = True
                    await asyncio.sleep(retry_after)
                    continue
                current_max = int(request_kwargs.get("max_tokens") or 0)
                while (
                    quota_retry_index < len(BUILDER_QUOTA_RETRY_TOKEN_STEPS)
                    and BUILDER_QUOTA_RETRY_TOKEN_STEPS[quota_retry_index] >= current_max
                ):
                    quota_retry_index += 1
                if quota_retry_index < len(BUILDER_QUOTA_RETRY_TOKEN_STEPS):
                    # Free-tier TPD accounting can reject a large requested completion
                    # even when a small patch still fits. Progressively reduce only this
                    # builder turn before advancing the approved model allowlist.
                    request_kwargs = dict(request_kwargs)
                    request_kwargs["max_tokens"] = BUILDER_QUOTA_RETRY_TOKEN_STEPS[
                        quota_retry_index
                    ]
                    quota_retry_index += 1
                    if request_kwargs.get("messages"):
                        request_kwargs["messages"] = _fit_builder_history(
                            request_kwargs["messages"],
                            max_chars=BUILDER_413_RETRY_MAX_CHARS,
                        )
                    continue
                break
            except APIStatusError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 404:
                    # Model availability is organization- and lifecycle-specific.
                    # Continue only within the configured builder-only allowlist.
                    last_error = exc
                    break
                bad_request_code = groq_bad_request_code(exc) if status == 400 else None
                if (
                    status in {400, 413} and not retried_oversize
                    and request_kwargs.get("messages")
                    and (status == 413 or bad_request_code == "model_context_length")
                ):
                    retried_oversize = True
                    request_kwargs = dict(request_kwargs)
                    request_kwargs["messages"] = _fit_builder_history(
                        request_kwargs["messages"], max_chars=BUILDER_413_RETRY_MAX_CHARS,
                    )
                    request_kwargs["max_tokens"] = min(
                        int(request_kwargs.get("max_tokens") or 2048), 2048,
                    )
                    continue
                if status == 400 and is_tool_generation_failure(exc):
                    if protocol_mode:
                        if not retried_json_generation:
                            # JSON response validation can itself reject an otherwise
                            # usable action object. Keep the JSON action instructions,
                            # remove provider response validation, and parse locally.
                            retried_json_generation = True
                            request_kwargs = dict(request_kwargs)
                            request_kwargs.pop("response_format", None)
                            request_kwargs["temperature"] = 0.0
                            continue
                        last_error = exc
                        break
                    if not retried_tool_generation:
                        retried_tool_generation = True
                        request_kwargs = dict(request_kwargs)
                        request_kwargs["temperature"] = 0.0
                        request_kwargs["parallel_tool_calls"] = False
                        request_kwargs["disable_tool_validation"] = True
                        continue
                    # Some Groq models repeatedly fail while serializing a native tool
                    # call. Preserve the same local authority boundary while removing
                    # provider tool syntax from the generation path.
                    protocol_mode = True
                    kwargs = _json_tool_protocol_kwargs(request_kwargs)
                    request_kwargs = kwargs
                    continue
                if status == 400:
                    # A 400 can be model-specific (context/output limits or feature
                    # support). Advance only through the configured builder allowlist;
                    # never alter the models used by ordinary user workflows.
                    last_error = exc
                    break
                raise
    if last_error is None:
        raise RuntimeError("Candidate model chain ended without a provider result")
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
    for message in fitted:
        if message.get("role") != "user":
            continue
        try:
            content = json.loads(message.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        result = content.get("tool_result") if isinstance(content, dict) else None
        if not isinstance(result, dict):
            continue
        message["content"] = json.dumps({
            "tool_result": {
                "name": result.get("name"),
                "call_id": result.get("call_id"),
                "compacted": True,
                "reason": "earlier JSON protocol result removed to preserve request budget",
            },
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


def candidate_contract_errors(candidate: dict) -> list[str]:
    """Return bounded, content-free reasons a draft cannot enter the API."""
    errors: list[str] = []
    files = candidate.get("files")
    if not isinstance(files, list) or not files:
        return ["files_required"]
    if len(files) > 50:
        errors.append("too_many_files")
    structurally_valid = []
    for item in files[:50]:
        if not isinstance(item, dict):
            errors.append("file_entry_not_object")
            continue
        path = item.get("path")
        change_type = item.get("change_type")
        content = item.get("content")
        if not isinstance(path, str) or not path or len(path) > 500:
            errors.append("file_path_invalid")
            continue
        if change_type not in {"create", "replace", "delete"}:
            errors.append("file_change_type_invalid")
            continue
        if content is not None and not isinstance(content, str):
            errors.append("file_content_not_string")
            continue
        if isinstance(content, str) and len(content) > 500_000:
            errors.append("file_content_too_large")
            continue
        structurally_valid.append(item)
    if structurally_valid:
        for detail in validate_candidate_files(structurally_valid):
            if detail.startswith("Duplicate candidate path"):
                errors.append("duplicate_path")
            elif "outside approved roots" in detail or "Unsafe candidate path" in detail:
                errors.append("path_outside_approved_roots")
            elif "credentials" in detail or "secret-like" in detail:
                errors.append("secret_like_content")
            else:
                errors.append("candidate_file_policy_rejected")
    if not isinstance(candidate.get("rollback_plan"), dict):
        errors.append("rollback_plan_invalid")
    commands = candidate.get("validation_commands")
    if not isinstance(commands, list) or len(commands) > 50 or not all(
        isinstance(command, str) for command in commands
    ):
        errors.append("validation_commands_invalid")
    return list(dict.fromkeys(errors))


def reviewer_contract_errors(review: dict) -> list[str]:
    """Validate the review envelope without treating it as candidate files."""
    if not isinstance(review.get("approved"), bool):
        return ["review_approval_required"]
    revised = review.get("revised_candidate")
    if revised is not None and not isinstance(revised, dict):
        return ["revised_candidate_not_object"]
    if review["approved"] is False and not str(review.get("reason") or "").strip():
        return ["review_rejection_reason_required"]
    return []

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
    response, model, _ = await _candidate_completion(
        client, job,
        messages=[{"role": "user", "content": _candidate_prompt(job, sources, role)}],
        temperature=0.1,
        max_tokens=min(
            BUILDER_FINAL_TURN_MAX_TOKENS,
            settings.candidate_builder_max_output_tokens,
            effective_builder_token_budget(job),
        ),
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
    token_budget = effective_builder_token_budget(job)
    models_used: list[str] = []
    json_tool_protocol = False
    max_rounds = (
        BUILDER_REVIEWER_MAX_ROUNDS
        if role == "independent_safety_reviewer"
        else BUILDER_AUTHOR_MAX_ROUNDS
    )
    for round_number in range(max_rounds):
        if tokens >= token_budget:
            raise RuntimeError("Candidate token budget exhausted during tool reasoning")
        remaining = max(256, token_budget - tokens)
        messages = _fit_builder_history(messages)
        force_finalize = round_number >= max_rounds - 2
        if force_finalize:
            final_messages = _json_tool_protocol_kwargs({
                "messages": messages, "tools": [],
            })["messages"]
            final_messages.append({
                "role": "user",
                "content": json.dumps({
                    "finalization_required": True,
                    "instruction": (
                        "Repository investigation is closed. Return the reviewer envelope "
                        "now with approved, reason, and optional revised_candidate. Do not "
                        "request another tool."
                        if role == "independent_safety_reviewer" else
                        "Repository investigation is closed. Return the final candidate JSON "
                        "contract now using the evidence and staged files already available. "
                        "Do not request another tool."
                    ),
                    "staged_files": candidate_review_projection({
                        "files": tools.staged_files(),
                    })["files"],
                }),
            })
            response, model, _ = await _candidate_completion(
                client, job, messages=final_messages, temperature=0.1,
                max_tokens=min(
                    BUILDER_FINAL_TURN_MAX_TOKENS,
                    settings.candidate_builder_max_output_tokens,
                    remaining,
                ),
                response_format={"type": "json_object"},
            )
            json_tool_protocol = True
        else:
            response, model, json_tool_protocol = await _candidate_completion(
                client, job, messages=messages,
                tools=tools.schemas(), tool_choice="auto", temperature=0.1,
                max_tokens=min(
                    BUILDER_TOOL_TURN_MAX_TOKENS,
                    settings.candidate_builder_max_output_tokens,
                    remaining,
                ),
                json_tool_protocol=json_tool_protocol,
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
        protocol_call = candidate.get("tool_call") if json_tool_protocol else None
        if isinstance(protocol_call, dict):
            if force_finalize:
                messages.append({
                    "role": "user",
                    "content": json.dumps({
                        "tool_result": {
                            "name": protocol_call.get("name"),
                            "error": "repository tools are closed; finalize the candidate",
                        },
                    }),
                })
                continue
            name = str(protocol_call.get("name") or "")
            arguments = protocol_call.get("arguments")
            if not isinstance(arguments, dict):
                result = {
                    "error": "ValueError",
                    "detail": "JSON repository action arguments must be an object",
                }
            else:
                try:
                    result = tools.execute(name, arguments)
                except Exception as exc:
                    result = {"error": type(exc).__name__, "detail": str(exc)[:500]}
            call = {
                "id": f"json-protocol-{round_number}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments or {})},
            }
            messages.append({
                "role": "assistant",
                "content": json.dumps({"tool_calls": [_compact_builder_tool_call(call)]}),
            })
            messages.append({
                "role": "user",
                "content": json.dumps({
                    "tool_result": {
                        "name": name,
                        "result": tools.project_result(name, result),
                    },
                }, default=str),
            })
            continue
        if role == "independent_safety_reviewer":
            review_errors = reviewer_contract_errors(candidate)
            if review_errors:
                if round_number < max_rounds - 1:
                    messages.append({
                        "role": "user",
                        "content": json.dumps({
                            "review_contract_rejected": review_errors,
                            "instruction": (
                                "Return one corrected reviewer envelope with a boolean approved, "
                                "a reason when rejected, and an optional complete revised_candidate."
                            ),
                        }),
                    })
                    continue
                raise RuntimeError(
                    "Reviewer contract failed local validation: "
                    + ",".join(review_errors)
                )
            return candidate, tokens, models_used
        if tools.staged_files():
            candidate["files"] = tools.staged_files()
            candidate.setdefault("exact_diff", tools.diff()["diff"])
        candidate = normalize_candidate_contract(candidate)
        if not candidate.get("exact_diff"):
            candidate["exact_diff"] = (
                "Frozen candidate files are authoritative; trusted CI will compute the diff."
            )
        contract_errors = candidate_contract_errors(candidate)
        if contract_errors:
            if round_number < max_rounds - 1:
                messages.append({
                    "role": "user",
                    "content": json.dumps({
                        "candidate_contract_rejected": contract_errors,
                        "instruction": (
                            "Return one corrected final candidate JSON object now. Include at "
                            "least one complete file under an approved root; do not claim tests passed."
                        ),
                    }),
                })
                continue
            raise RuntimeError(
                "Candidate contract failed local validation: "
                + ",".join(contract_errors)
            )
        return candidate, tokens, models_used
    raise RuntimeError("Candidate builder exceeded its bounded reasoning/tool rounds")


async def generate_candidate_draft(
    job: dict,
    checkpoint_callback: Callable[[dict], Awaitable[None]] | None = None,
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
    resume = dict(job.get("generation_checkpoint") or {})
    checkpoint_files = list(job.get("checkpoint_files") or [])
    if not checkpoint_files:
        # A phase is resumable only when its validated frozen files exist too.
        resume = {}
    for item in checkpoint_files:
        repository_tools.stage(
            item["path"], item["change_type"], item.get("content") or "",
        )
    candidate = None
    completed_roles = list(resume.get("roles_completed") or [])
    if resume and checkpoint_files:
        candidate = normalize_candidate_contract({
            "files": repository_tools.staged_files(),
            "exact_diff": resume.get("exact_diff") or repository_tools.diff()["diff"],
            "rollback_plan": resume.get("rollback_plan") or {
                "action": "route traffic to base version",
            },
            "validation_commands": resume.get("validation_commands") or [],
        })
    tokens = int(resume.get("tokens_used") or 0)
    token_budget = effective_builder_token_budget(job)
    models_used: list[str] = list(resume.get("models_used") or [])
    for role in roles:
        if role in completed_roles:
            continue
        if tokens >= token_budget:
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
            if repository_tools.staged_files():
                candidate["files"] = repository_tools.staged_files()
                candidate["exact_diff"] = repository_tools.diff()["diff"]
        else:
            candidate = output
            if "independent_safety_reviewer" in roles:
                # Direct JSON files are normalized into the same bounded in-memory
                # staging area so the reviewer can inspect their complete content.
                for item in candidate.get("files") or []:
                    repository_tools.stage(
                        item["path"], item["change_type"], item.get("content") or "",
                    )
            completed_roles.append(role)
            if (
                checkpoint_callback is not None
                and "independent_safety_reviewer" in roles
            ):
                checkpoint_candidate = normalize_candidate_contract({
                    **candidate,
                    "files": repository_tools.staged_files(),
                    "exact_diff": repository_tools.diff()["diff"],
                })
                await checkpoint_callback({
                    "files": checkpoint_candidate["files"],
                    "exact_diff": checkpoint_candidate["exact_diff"],
                    "rollback_plan": checkpoint_candidate["rollback_plan"],
                    "validation_commands": checkpoint_candidate.get(
                        "validation_commands", []
                    ),
                    "roles_completed": completed_roles,
                    "models_used": models_used,
                    "tokens_used": tokens,
                })
    candidate = normalize_candidate_contract(candidate or {})
    candidate.setdefault("exact_diff", "generated files are the authoritative candidate")
    candidate.setdefault("rollback_plan", {"action": "route traffic to base version"})
    candidate.setdefault("validation_commands", [])
    final_errors = candidate_contract_errors(candidate)
    if final_errors:
        raise RuntimeError(
            "Candidate contract failed local validation: " + ",".join(final_errors)
        )
    return candidate, tokens, roles, models_used


async def store_candidate_checkpoint(
    pool, build_id, candidate: dict, tokens: int, roles: list[str],
    models_used: list[str] | None = None,
) -> dict:
    """Persist resumable untrusted author output without granting CI authority."""
    files = candidate.get("files") or []
    errors = validate_candidate_files(files)
    if errors:
        raise ValueError("; ".join(errors))
    async with pool.acquire() as conn, conn.transaction():
        job = await conn.fetchrow(
            "SELECT * FROM candidate_builds WHERE id=$1 FOR UPDATE", build_id,
        )
        if not job or job["status"] != "investigating":
            raise ValueError("Candidate build is unavailable for checkpointing")
        await conn.execute(
            "DELETE FROM candidate_build_files WHERE build_id=$1", job["id"],
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
        generation_checkpoint = {
            "phase": "author_completed",
            "roles_completed": roles,
            "models_used": models_used or [job["model_name"]],
            "tokens_used": int(tokens),
            "exact_diff": candidate["exact_diff"],
            "rollback_plan": candidate["rollback_plan"],
            "validation_commands": candidate.get("validation_commands") or [],
            "file_count": len(files),
            "contains_private_evidence": False,
        }
        await conn.execute(
            """UPDATE candidate_builds SET tokens_used=GREATEST(tokens_used,$1),
               checkpoint=checkpoint||$2::jsonb,updated_at=now() WHERE id=$3""",
            int(tokens), json.dumps({"generation_checkpoint": generation_checkpoint}),
            job["id"],
        )
    return {
        "build_id": str(build_id), "status": "investigating",
        "phase": "author_completed", "file_count": len(files),
    }


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
        await conn.execute(
            "DELETE FROM candidate_build_files WHERE build_id=$1", job["id"],
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
