"""Server-authoritative retry policy for governed candidate builds."""

from dataclasses import dataclass, field
from typing import Any

from app.improvements.builder import (
    BUILDER_AUTHOR_MAX_ROUNDS,
    BUILDER_REVIEWER_MAX_ROUNDS,
    effective_builder_token_budget,
)


@dataclass(frozen=True)
class CandidateRetryDecision:
    eligible: bool
    reason_code: str
    resume_point: dict[str, Any] = field(default_factory=dict)


_TERMINAL_CODES = frozenset({
    "secret_like_content",
    "path_outside_approved_roots",
    "candidate_file_policy_rejected",
    "independent_review_rejected",
    "invalid_checkpoint",
    "production_mutation_attempt",
    "reviewer_contract_invalid",
})
_PROVIDER_CODES = frozenset({
    "RateLimitError",
    "APITimeoutError",
    "APIConnectionError",
    "ConnectError",
    "ReadTimeout",
    "TimeoutError",
    "checkpointed_timeout",
    "model_context_length",
    "tool_generation_failed",
})


def _checkpoint(job: dict) -> dict:
    value = job.get("checkpoint") or {}
    if not isinstance(value, dict):
        return {}
    generation = value.get("generation_checkpoint") or {}
    return generation if isinstance(generation, dict) else {}


def candidate_retry_decision(
    job: dict, *, error_type: str | None = None,
    contract_errors: list[str] | None = None,
    runner_retryable: bool = False,
) -> CandidateRetryDecision:
    """Recompute eligibility from durable state; runner input is only a hint."""
    code = str(error_type or "")
    errors = {str(value) for value in (contract_errors or [])}
    if job.get("candidate_commit"):
        return CandidateRetryDecision(False, "candidate_commit_exists")
    if job.get("candidate_deployment_id"):
        return CandidateRetryDecision(False, "candidate_deployment_exists")
    if code in _TERMINAL_CODES or errors.intersection(_TERMINAL_CODES):
        return CandidateRetryDecision(False, code or sorted(errors)[0])

    checkpoint = _checkpoint(job)
    phase = checkpoint.get("phase")
    file_count = int(
        checkpoint.get("staged_file_count")
        or checkpoint.get("file_count")
        or job.get("file_count")
        or 0
    )
    active_role = str(checkpoint.get("active_role") or "")
    next_round = int(checkpoint.get("next_round") or 0)
    messages = checkpoint.get("messages")
    valid = (
        phase == "author_completed" and file_count > 0
    ) or (
        phase == "role_in_progress"
        and active_role in {
            "coordinator", "investigator_and_patch_author",
            "tool_extension_designer", "independent_safety_reviewer",
            "review_remediation_author",
        }
        and isinstance(messages, list) and bool(messages)
    )
    resume = {
        "phase": phase,
        "active_role": active_role or None,
        "next_round": next_round,
        "staged_file_count": file_count,
    }

    if code in _PROVIDER_CODES and runner_retryable:
        return CandidateRetryDecision(
            True,
            "provider_retry_from_checkpoint" if valid else "provider_retry_from_start",
            resume if valid else {"phase": "restart"},
        )
    if not valid:
        return CandidateRetryDecision(False, "no_valid_resumable_checkpoint", resume)
    max_rounds = (
        BUILDER_REVIEWER_MAX_ROUNDS
        if active_role == "independent_safety_reviewer"
        else BUILDER_AUTHOR_MAX_ROUNDS
    )
    if phase == "role_in_progress" and not 0 <= next_round < max_rounds:
        return CandidateRetryDecision(False, "round_authority_exhausted", resume)
    tokens_used = int(job.get("tokens_used") or checkpoint.get("tokens_used") or 0)
    if tokens_used >= effective_builder_token_budget(job):
        return CandidateRetryDecision(False, "effective_token_authority_exhausted", resume)
    if code == "tool_token_budget_exhausted":
        prior_failure = job.get("checkpoint") or {}
        if not isinstance(prior_failure, dict):
            prior_failure = {}
        prior_reason = (prior_failure.get("last_runner_failure") or {}).get(
            "retry_reason"
        )
        restart_count = max(
            int(checkpoint.get("budget_restart_count") or 0),
            1 if prior_reason == "compact_role_restart" else 0,
        )
        if runner_retryable and restart_count < 1:
            return CandidateRetryDecision(
                True,
                "compact_role_restart",
                {**resume, "phase": "compact_role_restart"},
            )
        return CandidateRetryDecision(False, "active_role_token_authority_exhausted", resume)
    if code == "files_required" and file_count == 0:
        return CandidateRetryDecision(False, "files_required", resume)
    return CandidateRetryDecision(True, "checkpointed_structural_retry", resume)


def compact_role_restart_checkpoint(job: dict) -> dict:
    """Create one bounded, auditable restart after an active role exhausts tokens.

    The cumulative candidate budget is preserved. Only the active role's verbose
    conversation is compacted, preventing a retry from immediately failing with
    zero role-token authority while also preventing unlimited fresh restarts.
    """
    checkpoint = _checkpoint(job)
    messages = checkpoint.get("messages") or []
    initial = next(
        (message for message in messages if message.get("role") == "user"),
        {"role": "user", "content": "Continue the bounded candidate task."},
    )
    evidence = {
        "resume_mode": "compact_role_restart",
        "instruction": (
            "Continue from this compact checkpoint. Re-read only the exact runtime "
            "symbols still needed, stage an integrated implementation and regression "
            "test early, then finalize. Do not repeat broad investigation."
        ),
        "prior_tool_calls": int(checkpoint.get("tool_calls") or 0),
        "prior_read_bytes": int(checkpoint.get("read_bytes") or 0),
        "read_paths": list(checkpoint.get("read_paths") or [])[:50],
        "staged_file_count": int(checkpoint.get("staged_file_count") or 0),
        "last_contract_errors": list(checkpoint.get("last_contract_errors") or [])[:20],
        "last_tool_name": checkpoint.get("last_tool_name"),
    }
    return {
        **checkpoint,
        "phase": "role_in_progress",
        "next_round": 0,
        "messages": [initial, {"role": "user", "content": str(evidence)}],
        "role_tokens_used": 0,
        "role_models_used": [],
        "budget_restart_count": int(checkpoint.get("budget_restart_count") or 0) + 1,
        "progress_gate": "compact_role_restart",
        "resume_point": f"{checkpoint.get('active_role') or 'role'}:compact-restart:0",
    }
