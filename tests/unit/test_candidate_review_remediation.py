from __future__ import annotations

import asyncio

import pytest

from app.improvements.builder import (
    CandidateBuilderFailure,
    bounded_review_feedback,
    generate_candidate_draft,
)
from app.improvements.retry import (
    candidate_retry_decision,
    compact_role_restart_checkpoint,
)


def _candidate(value: int = 1) -> dict:
    return {
        "files": [{
            "path": "tests/review_remediation_candidate.py",
            "change_type": "create",
            "content": f"value = {value}\n",
        }],
        "exact_diff": f"candidate value {value}",
        "rollback_plan": {"action": "remove candidate fixture"},
        "validation_commands": ["pytest -q tests/unit"],
    }


def test_review_feedback_is_bounded_and_redacts_identity_and_secrets():
    projected = bounded_review_feedback(
        "Contact private@example.com because api_key=unsafe-value "
        + ("x" * 3_000)
    )

    assert "[redacted-email]" in projected["reason"]
    assert "private@example.com" not in projected["reason"]
    assert "unsafe-value" not in projected["reason"]
    assert projected["characters"] == 2_000
    assert len(projected["sha256"]) == 64


def test_rejected_candidate_is_remediated_and_independently_reviewed_again(
    monkeypatch,
):
    calls = []
    checkpoints = []

    async def fake_role(
        job, tools, role, prior_candidate=None, review_feedback=None, **_kwargs,
    ):
        calls.append(role)
        if role == "investigator_and_patch_author":
            return _candidate(1), 10, ["author-model"]
        if role == "review_remediation_author":
            assert prior_candidate["files"][0]["content"] == "value = 1\n"
            assert review_feedback["source"] == "untrusted_independent_review"
            assert "Add a regression assertion" in review_feedback["reason"]
            return _candidate(2), 12, ["repair-model"]
        assert role == "independent_safety_reviewer"
        if calls.count(role) == 1:
            return {
                "approved": False,
                "reason": "Add a regression assertion before approval",
            }, 5, ["review-model"]
        assert prior_candidate["files"][0]["content"] == "value = 2\n"
        return {"approved": True, "reason": "corrected"}, 4, ["review-model"]

    async def checkpoint(payload):
        checkpoints.append(payload)

    monkeypatch.setattr(
        "app.improvements.builder._groq_tool_json", fake_role,
    )
    candidate, tokens, roles, models = asyncio.run(generate_candidate_draft({
        "mode": "multi_role",
        "model_name": "llama-3.3-70b-versatile",
        "token_budget": 48_000,
        "sanitized_input": {
            "component": "step_executor",
            "selected_option": {"change_scope": ["app", "tests"]},
        },
    }, checkpoint_callback=checkpoint))

    assert calls == [
        "investigator_and_patch_author",
        "independent_safety_reviewer",
        "review_remediation_author",
        "independent_safety_reviewer",
    ]
    assert candidate["files"][0]["content"] == "value = 2\n"
    assert tokens == 31
    assert roles == [
        "investigator_and_patch_author",
        "review_remediation_author",
        "independent_safety_reviewer",
    ]
    assert models == ["author-model", "review-model", "repair-model"]
    assert checkpoints[-1]["roles_completed"] == [
        "investigator_and_patch_author",
        "review_remediation_author",
    ]


def test_second_independent_rejection_fails_closed(monkeypatch):
    async def fake_role(job, tools, role, prior_candidate=None, **_kwargs):
        if role in {
            "investigator_and_patch_author", "review_remediation_author",
        }:
            return _candidate(1), 2, [role]
        return {"approved": False, "reason": "still unsafe"}, 2, ["reviewer"]

    monkeypatch.setattr(
        "app.improvements.builder._groq_tool_json", fake_role,
    )
    with pytest.raises(CandidateBuilderFailure) as raised:
        asyncio.run(generate_candidate_draft({
            "mode": "multi_role",
            "model_name": "llama-3.3-70b-versatile",
            "token_budget": 48_000,
            "sanitized_input": {
                "component": "step_executor",
                "selected_option": {"change_scope": ["app", "tests"]},
            },
        }))

    assert raised.value.safe_code == "independent_review_rejected"
    assert raised.value.contract_errors == ["review_rejected_after_remediation"]
    assert raised.value.terminal_policy is True


def test_review_remediation_role_is_a_valid_provider_resume_point():
    job = {
        "model_name": "llama-3.3-70b-versatile",
        "token_budget": 48_000,
        "tokens_used": 20_000,
        "candidate_commit": None,
        "candidate_deployment_id": None,
        "checkpoint": {"generation_checkpoint": {
            "phase": "role_in_progress",
            "active_role": "review_remediation_author",
            "next_round": 0,
            "messages": [{"role": "user", "content": "bounded repair prompt"}],
            "file_count": 2,
        }},
    }

    decision = candidate_retry_decision(
        job, error_type="RateLimitError", runner_retryable=True,
    )

    assert decision.eligible is True
    assert decision.reason_code == "provider_retry_from_checkpoint"
    assert decision.resume_point["active_role"] == "review_remediation_author"
    assert decision.resume_point["next_round"] == 0


def test_tool_budget_exhaustion_gets_one_compact_role_restart():
    job = {
        "model_name": "llama-3.3-70b-versatile",
        "token_budget": 12_000,
        "tokens_used": 13_982,
        "candidate_commit": None,
        "candidate_deployment_id": None,
        "checkpoint": {"generation_checkpoint": {
            "phase": "role_in_progress",
            "active_role": "investigator_and_patch_author",
            "next_round": 3,
            "messages": [
                {"role": "user", "content": "original bounded prompt"},
                {"role": "assistant", "content": "a very verbose investigation"},
            ],
            "file_count": 0,
            "tool_calls": 21,
            "read_bytes": 1_341,
            "read_paths": ["app/tools/registry.py"],
            "role_tokens_used": 13_982,
        }},
    }

    decision = candidate_retry_decision(
        job, error_type="tool_token_budget_exhausted", runner_retryable=True,
    )
    compact = compact_role_restart_checkpoint(job)

    assert decision.eligible is True
    assert decision.reason_code == "compact_role_restart"
    assert compact["role_tokens_used"] == 0
    assert compact["budget_restart_count"] == 1
    assert compact["next_round"] == 0
    assert compact["tool_calls"] == 21
    assert compact["read_paths"] == ["app/tools/registry.py"]
    assert len(compact["messages"]) == 2

    job["checkpoint"]["generation_checkpoint"] = compact
    terminal = candidate_retry_decision(
        job, error_type="tool_token_budget_exhausted", runner_retryable=True,
    )
    assert terminal.eligible is False
    assert terminal.reason_code == "active_role_token_authority_exhausted"


def test_prior_compact_retry_reason_prevents_restart_when_legacy_progress_dropped_count():
    job = {
        "model_name": "llama-3.3-70b-versatile",
        "token_budget": 12_000,
        "tokens_used": 29_724,
        "candidate_commit": None,
        "candidate_deployment_id": None,
        "checkpoint": {
            "last_runner_failure": {"retry_reason": "compact_role_restart"},
            "generation_checkpoint": {
                "phase": "role_in_progress",
                "active_role": "investigator_and_patch_author",
                "next_round": 3,
                "messages": [{"role": "user", "content": "bounded prompt"}],
                "file_count": 0,
                "role_tokens_used": 13_000,
            },
        },
    }

    decision = candidate_retry_decision(
        job, error_type="tool_token_budget_exhausted", runner_retryable=True,
    )

    assert decision.eligible is False
    assert decision.reason_code == "active_role_token_authority_exhausted"


def test_trusted_ci_feedback_starts_from_frozen_files_and_remediates(monkeypatch):
    calls = []

    async def fake_role(
        job, tools, role, prior_candidate=None, review_feedback=None, **_kwargs,
    ):
        calls.append(role)
        if role == "review_remediation_author":
            assert prior_candidate["files"][0]["content"] == "value = 1\n"
            assert (
                job["sanitized_input"]["trusted_ci_feedback"]["diagnostic_codes"]
                == ["backend_validation_failed"]
            )
            return _candidate(2), 8, ["repair-model"]
        assert prior_candidate["files"][0]["content"] == "value = 2\n"
        return {"approved": True, "reason": "CI defect corrected"}, 3, ["review-model"]

    monkeypatch.setattr("app.improvements.builder._groq_tool_json", fake_role)
    candidate, tokens, roles, models = asyncio.run(generate_candidate_draft({
        "mode": "multi_role",
        "model_name": "llama-3.3-70b-versatile",
        "token_budget": 12_000,
        "sanitized_input": {
            "component": "planner",
            "selected_option": {"change_scope": ["app", "tests"]},
            "trusted_ci_feedback": {
                "diagnostic_codes": ["backend_validation_failed"],
                "contains_raw_user_content": False,
            },
        },
        "checkpoint_files": _candidate(1)["files"],
    }))

    assert calls == [
        "review_remediation_author", "independent_safety_reviewer",
    ]
    assert candidate["files"][0]["content"] == "value = 2\n"
    assert tokens == 11
    assert roles == [
        "review_remediation_author", "independent_safety_reviewer",
    ]
    assert models == ["repair-model", "review-model"]
