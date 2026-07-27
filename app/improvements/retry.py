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
    if code == "files_required" and file_count == 0:
        return CandidateRetryDecision(False, "files_required", resume)
    return CandidateRetryDecision(True, "checkpointed_structural_retry", resume)
