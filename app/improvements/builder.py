"""Groq-only, no-execution candidate generation with durable checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from groq import APIStatusError, AsyncGroq, RateLimitError

from app.config.settings import get_settings
from app.improvements.candidates import (
    ALLOWED_ROOTS, candidate_digest, file_digest, infer_candidate_kind,
    validate_candidate_adoption, validate_candidate_files,
)
from app.improvements.builder_tools import BoundedRepositoryTools
from app.mlops.metrics import (
    candidate_checkpoint_resumes, candidate_progress_gates,
)

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
MODEL_POLICY_VERSION = "adaptive-roles-v3-model-chain-evidence"
TOOL_POLICY_VERSION = "bounded-repo-tools-v10-symbol-patch-sandbox"
BUILDER_HISTORY_MAX_CHARS = 24_000
BUILDER_413_RETRY_MAX_CHARS = 12_000
BUILDER_AUTHOR_MAX_ROUNDS = 8
BUILDER_REVIEWER_MAX_ROUNDS = 5
BUILDER_TOOL_TURN_MAX_TOKENS = 2_048
BUILDER_FINAL_TURN_MAX_TOKENS = 4_096
BUILDER_QUOTA_RETRY_TOKEN_STEPS = (1_024, 512, 256)
BUILDER_AUTHOR_EARLY_FILE_ROUND = 3
BUILDER_AUTHOR_RESTRICTED_ROUND = 5
BUILDER_AUTHOR_HARD_FILE_ROUND = 7
BUILDER_MAX_REVIEW_REMEDIATIONS = 1
BUILDER_CORRECTION_RESERVE_TOKENS = 1_024
BUILDER_FINALIZATION_RESERVE_TOKENS = 4_096
BUILDER_FINISH_TOOL_NAMES = frozenset({
    "stage_candidate_file",
    "apply_candidate_patch",
    "inspect_candidate_diff",
    "validate_staged_candidate",
    "inspect_candidate_manifest",
    "discard_staged_candidate_file",
    "read_staged_candidate_file",
})


@dataclass
class CandidateBuilderFailure(RuntimeError):
    """Typed, content-free builder failure used across runner and server boundaries."""

    safe_code: str
    contract_errors: list[str] = field(default_factory=list)
    role: str | None = None
    round_number: int | None = None
    staged_file_count: int = 0
    retry_class: str = "structural"
    terminal_policy: bool = False
    resume_point: str | None = None

    def __post_init__(self):
        RuntimeError.__init__(self, self.safe_code)


def builder_budget_snapshot(
    job: dict, cumulative_tokens: int, active_role_tokens: int,
    active_role_budget: int,
) -> dict:
    """Report distinct stored, effective, cumulative, and active-role budgets."""
    base = int(job["token_budget"])
    effective = effective_builder_token_budget(job)
    return {
        "base_token_budget": base,
        "effective_token_budget": effective,
        "tokens_used": max(0, int(cumulative_tokens)),
        "role_tokens_used": max(0, int(active_role_tokens)),
        "active_role_token_budget": max(0, int(active_role_budget)),
        "remaining_effective_tokens": max(0, effective - int(cumulative_tokens)),
        "remaining_active_role_tokens": max(
            0, int(active_role_budget) - int(active_role_tokens),
        ),
    }


def _restricted_builder_schemas(schemas: list[dict]) -> list[dict]:
    return [
        schema for schema in schemas
        if (schema.get("function") or {}).get("name") in BUILDER_FINISH_TOOL_NAMES
    ]


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


def _omitted_builder_output(content: str) -> str:
    """Retain only provenance for invalid generated text in durable history."""
    value = str(content or "")
    return json.dumps({
        "generated_output_omitted": True,
        "chars": len(value),
        "sha256": hashlib.sha256(value.encode()).hexdigest(),
    }, sort_keys=True)


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
    def json_protocol_calls(message: dict) -> list[dict]:
        if message.get("role") != "assistant" or message.get("tool_calls"):
            return []
        try:
            content = json.loads(message.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            return []
        calls = content.get("tool_calls") if isinstance(content, dict) else None
        return calls if isinstance(calls, list) else []

    def json_protocol_result(message: dict) -> bool:
        if message.get("role") != "user":
            return False
        try:
            content = json.loads(message.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(content, dict)
            and isinstance(content.get("tool_result"), dict)
        )

    index = 0
    while index < len(fitted):
        message = fitted[index]
        calls = (
            message.get("tool_calls")
            if message.get("role") == "assistant"
            else None
        ) or json_protocol_calls(message)
        if not calls:
            index += 1
            continue
        end = index + 1
        native_exchange = bool(message.get("tool_calls"))
        while end < len(fitted) and (
            fitted[end].get("role") == "tool"
            if native_exchange
            else json_protocol_result(fitted[end])
        ):
            end += 1
        names = [
            str((call.get("function") or {}).get("name") or "")[:100]
            for call in calls[:30]
            if isinstance(call, dict)
        ]
        fitted[index:end] = [{
            "role": "user",
            "content": json.dumps({
                "prior_tool_exchange_compacted": True,
                "tool_call_count": len(calls),
                "tool_names": names,
                "reason": "completed exchange removed to preserve request budget",
            }, sort_keys=True),
        }]
        if size() <= max_chars:
            return fitted
        index += 1
    # Repeated resumptions can leave several already-compacted exchanges. Merge
    # their safe provenance so the number of resumptions cannot itself exhaust
    # the request budget.
    compacted_indexes = []
    compacted_call_count = 0
    compacted_names: set[str] = set()
    for index, message in enumerate(fitted):
        if message.get("role") != "user":
            continue
        try:
            content = json.loads(message.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not (
            isinstance(content, dict)
            and content.get("prior_tool_exchange_compacted") is True
        ):
            continue
        compacted_indexes.append(index)
        compacted_call_count += int(content.get("tool_call_count") or 0)
        compacted_names.update(
            str(name)[:100] for name in content.get("tool_names") or []
        )
    if len(compacted_indexes) > 1:
        first = compacted_indexes[0]
        fitted[first] = {
            "role": "user",
            "content": json.dumps({
                "prior_tool_exchanges_compacted": True,
                "exchange_count": len(compacted_indexes),
                "tool_call_count": compacted_call_count,
                "tool_names": sorted(compacted_names)[:30],
                "reason": "prior compacted exchanges consolidated for request budget",
            }, sort_keys=True),
        }
        for index in reversed(compacted_indexes[1:]):
            del fitted[index]
        if size() <= max_chars:
            return fitted
    # Contract-invalid model outputs are normally replaced before persistence,
    # but older checkpoints and provider protocol conversions may contain a
    # verbose assistant-only turn. Preserve its provenance, never its body.
    for index in range(1, max(1, len(fitted) - 1)):
        message = fitted[index]
        if message.get("role") != "assistant" or message.get("tool_calls"):
            continue
        content = str(message.get("content") or "")
        if not content:
            continue
        message["content"] = _omitted_builder_output(content)
        if size() <= max_chars:
            return fitted
    for index in range(1, max(1, len(fitted) - 1)):
        message = fitted[index]
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        message["content"] = json.dumps({
            "prior_user_instruction_compacted": True,
            "chars": len(content),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }, sort_keys=True)
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
        })
    exact_diff = str(value.get("exact_diff") or "")
    return {
        "files": files,
        "exact_diff_chars": len(exact_diff),
        "exact_diff_sha256": hashlib.sha256(exact_diff.encode()).hexdigest(),
        "rollback_plan": value.get("rollback_plan"),
        "validation_commands": value.get("validation_commands") or [],
        "full_content_access": "use read_staged_candidate_file",
    }


def bounded_review_feedback(reason: object) -> dict:
    """Retain actionable model feedback without accepting secrets or raw identities."""
    text = str(reason or "").strip()
    text = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[redacted-email]", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|refresh[_-]?token|"
        r"client[_-]?secret)\s*[:=]\s*\S+",
        r"\1=[redacted]", text,
    )
    bounded = text[:2_000]
    return {
        "reason": bounded,
        "characters": len(bounded),
        "sha256": hashlib.sha256(bounded.encode()).hexdigest(),
        "source": "untrusted_independent_review",
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
        for detail in (
            validate_candidate_files(structurally_valid)
            + validate_candidate_adoption(structurally_valid)
        ):
            if detail.startswith("Duplicate candidate path"):
                errors.append("duplicate_path")
            elif "outside approved roots" in detail or "Unsafe candidate path" in detail:
                errors.append("path_outside_approved_roots")
            elif "credentials" in detail or "secret-like" in detail:
                errors.append("secret_like_content")
            elif "must be integrated" in detail:
                errors.append("runtime_integration_required")
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


def staged_validation_codes(validation: dict) -> list[str]:
    """Project deterministic pre-review failures into content-free contract codes."""
    codes = []
    for error in validation.get("errors") or []:
        code = str(error.get("code") or "")
        if code.endswith("_invalid"):
            codes.append("candidate_syntax_invalid")
        elif code == "candidate_policy":
            codes.append("candidate_file_policy_rejected")
        else:
            codes.append("candidate_structural_validation_failed")
    return list(dict.fromkeys(codes))


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
    remediating = role == "review_remediation_author"
    return json.dumps({
        "role": role,
        "objective": (
            "Independently reject or revise the supplied candidate. Return JSON only with "
            "approved, reason, and optional revised_candidate."
            if reviewing else (
                "Remediate the frozen candidate against the bounded independent-review "
                "feedback. Inspect exact staged files as needed, stage complete corrected "
                "files, validate their structure, and return the complete candidate contract."
                if remediating else
            "Inspect through the bounded repository tools, stage a minimal implementation and "
            "regression tests, then return JSON only with files[{path,change_type,content}], "
            "exact_diff, rollback_plan, validation_commands, notes."
            )
        ),
        "rules": [
            "Use only supplied repository paths or create files under app/, tests/, knowledge/, config/, docs/.",
            "Never include secrets, credentials, raw user content, shell commands, network calls, or production mutations.",
            "Do not claim tests passed. CI validates the frozen candidate separately.",
            "Preserve unrelated behavior and include a no-network regression for the reported failure.",
            "Tool calls can only read or stage in memory; they cannot execute, publish, or authorize changes.",
            "Prefer symbol indexing, symbol reads, reference lookup, test-neighborhood lookup, and bounded line patches over reading or emitting whole files.",
            "Use structural validation as a local compiler check; trusted no-secret CI remains the only test authority.",
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
    progress: dict | None = None,
    progress_callback: Callable[[dict], Awaitable[None]] | None = None,
    role_token_budget: int | None = None,
    cumulative_tokens_before: int = 0,
    review_feedback: dict | None = None,
    checkpoint_at_start: bool = False,
) -> tuple[dict, int, list[str]]:
    """Run a bounded tool loop; Groq never receives a shell or network tool."""
    settings = get_settings()
    client = AsyncGroq(api_key=settings.groq_api_key)
    resume = dict(progress or {})
    if resume and resume.get("active_role") != role:
        raise CandidateBuilderFailure(
            "invalid_checkpoint", role=role, terminal_policy=True,
        )
    sources = (
        [{"candidate_for_revision": candidate_review_projection(prior_candidate)}]
        if prior_candidate else [{
            "repository": "ephemeral checkout",
            "approved_roots": list(ALLOWED_ROOTS),
            "read_limit_bytes": tools.max_read_bytes,
            "tool_call_limit": tools.max_calls,
            "changed_file_limit": tools.max_files,
        }]
    )
    if review_feedback:
        sources.append({"review_feedback": review_feedback})
    initial_messages = [{
        "role": "user",
        "content": _candidate_prompt(job, sources, role),
    }]
    messages = json.loads(json.dumps(resume.get("messages") or initial_messages))
    tokens = int(resume.get("role_tokens_used") or 0)
    initial_tokens = tokens
    token_budget = min(
        effective_builder_token_budget(job),
        int(
            effective_builder_token_budget(job)
            if role_token_budget is None else role_token_budget
        ),
    )
    models_used: list[str] = list(resume.get("role_models_used") or [])
    json_tool_protocol = bool(resume.get("json_tool_protocol"))
    max_rounds = (
        BUILDER_REVIEWER_MAX_ROUNDS
        if role == "independent_safety_reviewer"
        else BUILDER_AUTHOR_MAX_ROUNDS
    )
    start_round = int(resume.get("next_round") or 0)
    if start_round > max_rounds:
        raise CandidateBuilderFailure(
            "invalid_checkpoint", role=role, round_number=start_round,
            terminal_policy=True,
        )
    if resume:
        candidate_checkpoint_resumes.labels(
            role, str(resume.get("phase") or "role_in_progress"),
        ).inc()
        tools.restore_counters(
            calls=int(resume.get("tool_calls") or 0),
            read_bytes=int(resume.get("read_bytes") or 0),
        )

    last_tool_name = str(resume.get("last_tool_name") or "") or None
    last_contract_errors = [
        str(value)[:100] for value in resume.get("last_contract_errors", [])[:20]
    ]
    progress_gate = str(resume.get("progress_gate") or "investigating")

    async def emit_progress(next_round: int) -> None:
        if progress_callback is None:
            return
        fitted = _fit_builder_history(messages)
        if len(json.dumps(fitted, default=str)) > BUILDER_HISTORY_MAX_CHARS:
            raise RuntimeError("Candidate checkpoint history exceeds its bounded size")
        active_used = tokens
        cumulative = cumulative_tokens_before + tokens - initial_tokens
        await progress_callback({
            "phase": "role_in_progress",
            "active_role": role,
            "next_round": next_round,
            "messages": fitted,
            "json_tool_protocol": json_tool_protocol,
            "tool_calls": tools.calls,
            "read_bytes": tools.read_bytes,
            "role_models_used": models_used,
            "staged_file_count": len(tools.staged_files()),
            "last_contract_errors": last_contract_errors,
            "last_tool_name": last_tool_name,
            "progress_gate": progress_gate,
            "resume_point": f"{role}:round:{next_round}",
            **builder_budget_snapshot(
                job, cumulative, active_used, token_budget,
            ),
        })

    if progress_callback is not None and not resume and checkpoint_at_start:
        await emit_progress(start_round)

    for round_number in range(start_round, max_rounds):
        if tokens >= token_budget:
            raise CandidateBuilderFailure(
                "tool_token_budget_exhausted", role=role,
                round_number=round_number,
                staged_file_count=len(tools.staged_files()),
                retry_class="budget",
                resume_point=f"{role}:round:{round_number}",
            )
        remaining = max(256, token_budget - tokens)
        messages = _fit_builder_history(messages)
        reviewing = role == "independent_safety_reviewer"
        staged_count = len(tools.staged_files())
        if not reviewing and not staged_count:
            if round_number == BUILDER_AUTHOR_EARLY_FILE_ROUND:
                progress_gate = "file_required_correction"
                candidate_progress_gates.labels(role, progress_gate).inc()
                messages.append({
                    "role": "user",
                    "content": json.dumps({
                        "progress_gate": progress_gate,
                        "instruction": (
                            "Stage at least one complete candidate file now before any "
                            "additional broad repository investigation."
                        ),
                    }),
                })
            elif round_number >= BUILDER_AUTHOR_RESTRICTED_ROUND:
                if progress_gate != "finish_tools_only":
                    progress_gate = "finish_tools_only"
                    candidate_progress_gates.labels(role, progress_gate).inc()
        reserve = (
            BUILDER_CORRECTION_RESERVE_TOKENS
            + BUILDER_FINALIZATION_RESERVE_TOKENS
        )
        force_finalize = (
            round_number >= max_rounds - 2
            or (
                token_budget >= reserve
                and remaining <= reserve
                and (reviewing or staged_count > 0)
            )
        )
        if force_finalize:
            if progress_gate != "finalization":
                progress_gate = "finalization"
                candidate_progress_gates.labels(role, progress_gate).inc()
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
            available_schemas = tools.schemas()
            if not reviewing and round_number >= BUILDER_AUTHOR_RESTRICTED_ROUND:
                available_schemas = _restricted_builder_schemas(available_schemas)
            response, model, json_tool_protocol = await _candidate_completion(
                client, job, messages=messages,
                tools=available_schemas, tool_choice="auto", temperature=0.1,
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
                last_tool_name = call.function.name
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
            await emit_progress(round_number + 1)
            continue
        try:
            candidate = json.loads(message.content or "{}")
        except json.JSONDecodeError as exc:
            if round_number >= max_rounds - 1:
                raise RuntimeError("Groq candidate output was not valid JSON") from exc
            messages.append({
                "role": "assistant",
                "content": _omitted_builder_output(message.content or ""),
            })
            messages.append({
                "role": "user",
                "content": json.dumps({
                    "candidate_contract_rejected": ["invalid_json"],
                    "instruction": "Return one valid JSON object without Markdown fences.",
                }),
            })
            await emit_progress(round_number + 1)
            continue
        protocol_call = candidate.get("tool_call") if json_tool_protocol else None
        if isinstance(protocol_call, dict):
            if force_finalize:
                if round_number >= max_rounds - 1:
                    raise RuntimeError(
                        "Candidate requested a repository tool after finalization closed"
                    )
                messages.append({
                    "role": "user",
                    "content": json.dumps({
                        "tool_result": {
                            "name": protocol_call.get("name"),
                            "error": "repository tools are closed; finalize the candidate",
                        },
                    }),
                })
                await emit_progress(round_number + 1)
                continue
            name = str(protocol_call.get("name") or "")
            last_tool_name = name or None
            result = None
            if (
                not reviewing
                and round_number >= BUILDER_AUTHOR_RESTRICTED_ROUND
                and name not in BUILDER_FINISH_TOOL_NAMES
            ):
                result = {
                    "error": "progress_gate",
                    "detail": "Only staging and final validation tools remain available",
                }
                arguments = protocol_call.get("arguments")
            else:
                arguments = protocol_call.get("arguments")
            if result is None and not isinstance(arguments, dict):
                result = {
                    "error": "ValueError",
                    "detail": "JSON repository action arguments must be an object",
                }
            elif result is None:
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
            await emit_progress(round_number + 1)
            continue
        messages.append({
            "role": "assistant",
            "content": _omitted_builder_output(message.content or ""),
        })
        if role == "independent_safety_reviewer":
            review_errors = reviewer_contract_errors(candidate)
            last_contract_errors = review_errors
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
                    await emit_progress(round_number + 1)
                    continue
                raise CandidateBuilderFailure(
                    "reviewer_contract_invalid", contract_errors=review_errors,
                    role=role, round_number=round_number,
                    staged_file_count=len(tools.staged_files()),
                    terminal_policy=True,
                )
            return candidate, tokens - initial_tokens, models_used
        direct_stage_errors = []
        direct_files = candidate.get("files")
        if isinstance(direct_files, list):
            for item in direct_files[:50]:
                if not isinstance(item, dict):
                    continue
                try:
                    tools.stage(
                        str(item.get("path") or ""),
                        str(item.get("change_type") or ""),
                        str(item.get("content") or ""),
                    )
                except (ValueError, TypeError) as exc:
                    direct_stage_errors.append(
                        "projected_candidate_body_rejected"
                        if "Projected staged-file provenance" in str(exc)
                        else "candidate_file_policy_rejected"
                    )
                except RuntimeError:
                    direct_stage_errors.append("candidate_output_limit_exceeded")
        if tools.staged_files():
            candidate["files"] = tools.staged_files()
            candidate.setdefault("exact_diff", tools.diff()["diff"])
        candidate = normalize_candidate_contract(candidate)
        if not candidate.get("exact_diff"):
            candidate["exact_diff"] = (
                "Frozen candidate files are authoritative; trusted CI will compute the diff."
            )
        contract_errors = candidate_contract_errors(candidate)
        contract_errors.extend(direct_stage_errors)
        contract_errors.extend(staged_validation_codes(tools.validate_staged()))
        contract_errors = list(dict.fromkeys(contract_errors))
        last_contract_errors = contract_errors
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
                await emit_progress(round_number + 1)
                continue
            raise CandidateBuilderFailure(
                "candidate_contract_invalid", contract_errors=contract_errors,
                role=role, round_number=round_number,
                staged_file_count=len(tools.staged_files()),
                retry_class="structural",
                resume_point=f"{role}:round:{round_number}",
            )
        return candidate, tokens - initial_tokens, models_used
    raise CandidateBuilderFailure(
        "tool_round_limit_exhausted", role=role, round_number=max_rounds,
        staged_file_count=len(tools.staged_files()), retry_class="round_limit",
        resume_point=f"{role}:round:{max_rounds}",
    )


async def generate_candidate_draft(
    job: dict,
    checkpoint_callback: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[dict, int, list[str], list[str]]:
    """Generate a patch from sanitized facts and a bounded checkout only."""
    sanitized = dict(job["sanitized_input"] or {})
    ci_feedback = sanitized.get("trusted_ci_feedback")
    scope = sanitized.get("selected_option", {}).get("change_scope", [])
    tool_extension = any(
        "tool" in value.casefold() for value in [sanitized.get("component", ""), *scope]
    )
    first_role = "review_remediation_author" if ci_feedback else (
        "tool_extension_designer" if tool_extension else (
        "coordinator" if job["mode"] == "single" else "investigator_and_patch_author"
        )
    )
    roles = [first_role] + (
        ["independent_safety_reviewer"] if job["mode"] == "multi_role" else []
    )
    repository_tools = BoundedRepositoryTools(ROOT)
    resume = dict(job.get("generation_checkpoint") or {})
    checkpoint_files = list(job.get("checkpoint_files") or [])
    if resume.get("phase") == "author_completed" and not checkpoint_files:
        # A completed phase is resumable only when its frozen files exist too.
        resume = {}
    elif resume.get("phase") == "role_in_progress" and not resume.get("messages"):
        # A partial role needs bounded conversation state; files alone are ambiguous.
        resume = {}
    for item in checkpoint_files:
        repository_tools.stage(
            item["path"], item["change_type"], item.get("content") or "",
        )
    candidate = None
    completed_roles = list(resume.get("roles_completed") or [])
    if (
        resume.get("active_role") == "review_remediation_author"
        or "review_remediation_author" in completed_roles
    ):
        roles = [
            first_role,
            "review_remediation_author",
            "independent_safety_reviewer",
        ]
    if checkpoint_files and (
        ci_feedback
        or
        resume.get("phase") == "author_completed"
        or "investigator_and_patch_author" in resume.get("roles_completed", [])
        or "tool_extension_designer" in resume.get("roles_completed", [])
        or "coordinator" in resume.get("roles_completed", [])
    ):
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
    executed_roles = list(completed_roles)
    review_feedback = None
    role_index = 0
    while role_index < len(roles):
        role = roles[role_index]
        if role in completed_roles:
            role_index += 1
            continue
        if tokens >= token_budget:
            raise CandidateBuilderFailure(
                "review_token_budget_exhausted", role=role,
                retry_class="budget", resume_point=f"{role}:round:0",
            )
        role_progress = (
            resume if resume.get("phase") == "role_in_progress"
            and resume.get("active_role") == role else None
        )
        if role == "independent_safety_reviewer":
            validation_codes = staged_validation_codes(
                repository_tools.validate_staged(),
            )
            if validation_codes:
                raise CandidateBuilderFailure(
                    "candidate_pre_review_validation_failed",
                    contract_errors=validation_codes,
                    role=role,
                    staged_file_count=len(repository_tools.staged_files()),
                    retry_class="structural",
                    terminal_policy=True,
                    resume_point=f"{role}:preflight",
                )
        initial_role_tokens = int(
            (role_progress or {}).get("role_tokens_used") or 0
        )

        async def checkpoint_role(progress: dict) -> None:
            if checkpoint_callback is None:
                return
            merged_models = list(models_used)
            for value in progress.get("role_models_used") or []:
                if value not in merged_models:
                    merged_models.append(value)
            staged = repository_tools.staged_files()
            await checkpoint_callback({
                **progress,
                "files": staged,
                "exact_diff": repository_tools.diff()["diff"] if staged else "",
                "rollback_plan": (
                    (candidate or {}).get("rollback_plan")
                    or {"action": "discard the untrusted candidate checkpoint"}
                ),
                "validation_commands": (
                    (candidate or {}).get("validation_commands") or []
                ),
                "roles_completed": completed_roles,
                "models_used": merged_models,
                "tokens_used": (
                    tokens + int(progress.get("role_tokens_used") or 0)
                    - initial_role_tokens
                ),
            })

        output, used, call_models = await _groq_tool_json(
            job, repository_tools, role, candidate,
            progress=role_progress, progress_callback=checkpoint_role,
            role_token_budget=token_budget - tokens + initial_role_tokens,
            cumulative_tokens_before=tokens,
            review_feedback=(
                review_feedback if role == "review_remediation_author" else None
            ),
            checkpoint_at_start=(
                role == "review_remediation_author" and role_progress is None
            ),
        )
        tokens += used
        for model in call_models:
            if model not in models_used:
                models_used.append(model)
        if role == "independent_safety_reviewer":
            if output.get("approved") is False:
                if (
                    "review_remediation_author" not in completed_roles
                    and BUILDER_MAX_REVIEW_REMEDIATIONS > 0
                ):
                    review_feedback = bounded_review_feedback(output.get("reason"))
                    roles[role_index:role_index + 1] = [
                        "review_remediation_author",
                        "independent_safety_reviewer",
                    ]
                    continue
                raise CandidateBuilderFailure(
                    "independent_review_rejected",
                    contract_errors=["review_rejected_after_remediation"],
                    role=role,
                    staged_file_count=len(repository_tools.staged_files()),
                    retry_class="policy",
                    terminal_policy=True,
                    resume_point=f"{role}:rejected",
                )
            candidate = output.get("revised_candidate") or candidate
            if repository_tools.staged_files():
                candidate["files"] = repository_tools.staged_files()
                candidate["exact_diff"] = repository_tools.diff()["diff"]
            if role not in executed_roles:
                executed_roles.append(role)
        else:
            candidate = output
            # Direct JSON files are normalized into the same bounded in-memory
            # staging area for durable freezing and optional independent review.
            for item in candidate.get("files") or []:
                repository_tools.stage(
                    item["path"], item["change_type"], item.get("content") or "",
                )
            completed_roles.append(role)
            if role not in executed_roles:
                executed_roles.append(role)
            if checkpoint_callback is not None:
                checkpoint_candidate = normalize_candidate_contract({
                    **candidate,
                    "files": repository_tools.staged_files(),
                    "exact_diff": repository_tools.diff()["diff"],
                })
                await checkpoint_callback({
                    "phase": "author_completed",
                    "files": checkpoint_candidate["files"],
                    "exact_diff": checkpoint_candidate["exact_diff"],
                    "rollback_plan": checkpoint_candidate["rollback_plan"],
                    "validation_commands": checkpoint_candidate.get(
                        "validation_commands", []
                    ),
                    "roles_completed": completed_roles,
                    "models_used": models_used,
                    "tokens_used": tokens,
                    "staged_file_count": len(checkpoint_candidate["files"]),
                    "last_contract_errors": [],
                    "last_tool_name": None,
                    "progress_gate": "author_complete",
                    "resume_point": "independent_safety_reviewer:round:0",
                    **builder_budget_snapshot(job, tokens, 0, token_budget - tokens),
                })
        role_index += 1
    candidate = normalize_candidate_contract(candidate or {})
    candidate.setdefault("exact_diff", "generated files are the authoritative candidate")
    candidate.setdefault("rollback_plan", {"action": "route traffic to base version"})
    candidate.setdefault("validation_commands", [])
    final_errors = candidate_contract_errors(candidate)
    if final_errors:
        raise CandidateBuilderFailure(
            "candidate_contract_invalid", contract_errors=final_errors,
            staged_file_count=len(candidate.get("files") or []),
            retry_class="structural",
        )
    return candidate, tokens, executed_roles, models_used


async def store_candidate_checkpoint(
    pool, build_id, candidate: dict, tokens: int, roles: list[str],
    models_used: list[str] | None = None,
) -> dict:
    """Persist resumable untrusted generation state without granting CI authority."""
    phase = str(candidate.get("phase") or "author_completed")
    files = candidate.get("files") or []
    errors = validate_candidate_files(files) if files else []
    if errors:
        raise ValueError("; ".join(errors))
    messages = candidate.get("messages") or []
    if phase == "author_completed":
        if not files or not roles or not candidate.get("exact_diff"):
            raise ValueError("Completed author checkpoint requires frozen candidate files")
    elif phase == "role_in_progress":
        active_role = str(candidate.get("active_role") or "")
        next_round = int(candidate.get("next_round") or 0)
        max_rounds = (
            BUILDER_REVIEWER_MAX_ROUNDS
            if active_role == "independent_safety_reviewer"
            else BUILDER_AUTHOR_MAX_ROUNDS
        )
        if (
            not active_role or not messages or not 0 <= next_round < max_rounds
            or active_role in roles
            or int(candidate.get("role_tokens_used") or 0) > int(tokens)
            or not 0 <= int(candidate.get("tool_calls") or 0) <= 30
            or not 0 <= int(candidate.get("read_bytes") or 0) <= 120_000
        ):
            raise ValueError("In-progress checkpoint counters or role are invalid")
        if any(
            not isinstance(message, dict)
            or message.get("role") not in {"system", "user", "assistant", "tool"}
            for message in messages
        ):
            raise ValueError("Candidate checkpoint contains an invalid message role")
        if len(json.dumps(messages, default=str)) > BUILDER_HISTORY_MAX_CHARS:
            raise ValueError("Candidate checkpoint history exceeds its bounded size")
    else:
        raise ValueError("Unknown candidate checkpoint phase")
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
            "phase": phase,
            "roles_completed": roles,
            "models_used": models_used or [job["model_name"]],
            "tokens_used": int(tokens),
            "exact_diff": candidate.get("exact_diff") or "",
            "rollback_plan": candidate.get("rollback_plan") or {},
            "validation_commands": candidate.get("validation_commands") or [],
            "file_count": len(files),
            "staged_file_count": len(files),
            "base_token_budget": int(
                candidate.get("base_token_budget") or job["token_budget"]
            ),
            "effective_token_budget": int(
                candidate.get("effective_token_budget")
                or effective_builder_token_budget(dict(job))
            ),
            "remaining_effective_tokens": max(
                0, int(
                    candidate.get("remaining_effective_tokens")
                    if candidate.get("remaining_effective_tokens") is not None
                    else effective_builder_token_budget(dict(job)) - int(tokens)
                ),
            ),
            "active_role_token_budget": int(
                candidate.get("active_role_token_budget") or 0
            ),
            "remaining_active_role_tokens": int(
                candidate.get("remaining_active_role_tokens") or 0
            ),
            "last_contract_errors": [
                str(value)[:100]
                for value in (candidate.get("last_contract_errors") or [])[:20]
            ],
            "last_tool_name": (
                str(candidate.get("last_tool_name"))[:100]
                if candidate.get("last_tool_name") else None
            ),
            "progress_gate": (
                str(candidate.get("progress_gate"))[:100]
                if candidate.get("progress_gate") else None
            ),
            "resume_point": (
                str(candidate.get("resume_point"))[:200]
                if candidate.get("resume_point") else None
            ),
            "contains_private_evidence": False,
        }
        if phase == "role_in_progress":
            generation_checkpoint.update({
                "active_role": candidate["active_role"],
                "next_round": int(candidate["next_round"]),
                "messages": messages,
                "json_tool_protocol": bool(candidate.get("json_tool_protocol")),
                "tool_calls": int(candidate.get("tool_calls") or 0),
                "read_bytes": int(candidate.get("read_bytes") or 0),
                "role_tokens_used": int(candidate.get("role_tokens_used") or 0),
                "role_models_used": candidate.get("role_models_used") or [],
            })
        await conn.execute(
            """UPDATE candidate_builds SET tokens_used=GREATEST(tokens_used,$1),
               checkpoint=checkpoint||$2::jsonb,updated_at=now() WHERE id=$3""",
            int(tokens), json.dumps({"generation_checkpoint": generation_checkpoint}),
            job["id"],
        )
    return {
        "build_id": str(build_id), "status": "investigating",
        "phase": phase, "file_count": len(files),
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
                "last_retry_dispatch": {
                    "state": "completed", "contains_private_evidence": False,
                },
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
        checkpoint_files = await conn.fetch(
            """SELECT path,change_type,content FROM candidate_build_files
               WHERE build_id=$1 ORDER BY path""",
            row["id"],
        )
        await conn.execute(
            "UPDATE candidate_builds SET status='investigating',updated_at=now() WHERE id=$1",
            row["id"],
        )
    job = dict(row)
    job["generation_checkpoint"] = (
        (job.get("checkpoint") or {}).get("generation_checkpoint") or {}
    )
    job["checkpoint_files"] = [dict(item) for item in checkpoint_files]
    latest_checkpoint: dict = {}
    try:
        async def checkpoint_embedded(payload: dict) -> None:
            nonlocal latest_checkpoint
            latest_checkpoint = dict(payload)
            await store_candidate_checkpoint(
                pool, job["id"], payload, int(payload.get("tokens_used") or 0),
                list(payload.get("roles_completed") or []),
                list(payload.get("models_used") or []),
            )

        candidate, tokens, roles, models_used = await asyncio.wait_for(
            generate_candidate_draft(job, checkpoint_callback=checkpoint_embedded),
            timeout=get_settings().candidate_builder_timeout_seconds,
        )
        await store_candidate_draft(
            pool, job["id"], candidate, tokens, roles, models_used,
        )
        return True
    except Exception as exc:
        logger.exception("Candidate build %s failed", job["id"])
        from app.improvements.retry import candidate_retry_decision
        safe_code = (
            exc.safe_code if isinstance(exc, CandidateBuilderFailure)
            else (
                "checkpointed_timeout"
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                and latest_checkpoint
                else type(exc).__name__
            )
        )
        contract_errors = (
            exc.contract_errors if isinstance(exc, CandidateBuilderFailure) else []
        )
        runner_retryable = not (
            isinstance(exc, CandidateBuilderFailure) and exc.terminal_policy
        )
        async with pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                "SELECT * FROM candidate_builds WHERE id=$1 FOR UPDATE", job["id"],
            )
            retry = candidate_retry_decision(
                dict(current), error_type=safe_code,
                contract_errors=contract_errors,
                runner_retryable=runner_retryable,
            )
            status = "queued" if retry.eligible else "failed"
            await conn.execute(
                """UPDATE candidate_builds SET status=$1,error_message=$2,
                   checkpoint=checkpoint||$3::jsonb,updated_at=now(),
                   completed_at=CASE WHEN $1='failed' THEN now() ELSE NULL END
                   WHERE id=$4""",
                status, f"Candidate builder stopped at guard {safe_code}.",
                json.dumps({
                    "last_runner_failure": {
                        "stage": "generation", "error_type": safe_code,
                        "retryable": retry.eligible,
                        "runner_retryable": runner_retryable,
                        "retry_reason": retry.reason_code,
                        "resume_point": retry.resume_point,
                        "contract_errors": contract_errors[:20],
                        "contains_private_evidence": False,
                    },
                }), job["id"],
            )
            proposal = await conn.fetchrow(
                "SELECT * FROM improvement_proposals WHERE id=$1", job["proposal_id"],
            )
            if proposal and not retry.eligible:
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
