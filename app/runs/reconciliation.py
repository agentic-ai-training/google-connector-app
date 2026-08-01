"""Bounded, evidence-based decisions for resuming a failed durable write."""

from dataclasses import dataclass, field
from typing import Any, Literal

from app.runs.verifier import verify_executions_detailed
from app.tools.contracts import (
    WRITE_TOOLS,
    attempted_required_tools,
    write_contract_for,
)


ReconciliationState = Literal[
    "safe_to_retry", "already_completed", "manual_required",
]


@dataclass(frozen=True)
class ReconciliationDecision:
    state: ReconciliationState
    reason_code: str
    resume_step_id: str | None
    evidence: dict[str, Any] = field(default_factory=dict)


_INTRINSICALLY_IDEMPOTENT_WRITES = frozenset({
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "share_drive_file",
    "move_drive_file",
    "trash_drive_file",
    "resolve_chat_destination",
})


def _tool_executions(step: dict[str, Any]) -> list[dict]:
    output = step.get("output_data") or {}
    executions = output.get("tool_executions", []) if isinstance(output, dict) else []
    return [dict(item) for item in executions if isinstance(item, dict)]


async def reconcile_failed_step(
    run: dict[str, Any],
    step: dict[str, Any],
    artifacts: list[dict[str, Any]] | None = None,
) -> ReconciliationDecision:
    """Reconcile one exact failed step without mutating database state."""
    step_id = str(step["id"])
    evidence = {
        "run_id": str(run["id"]),
        "step_id": step_id,
        "service": step.get("service"),
        "operation": step.get("operation"),
        "failure_category": step.get("error_category") or run.get("error_category"),
        "artifact_count": len(artifacts or []),
    }
    external_configuration_error = str(step.get("error_message") or "").casefold()
    if step.get("service") == "chat" and any(marker in external_configuration_error for marker in (
        "chat api configuration is incomplete",
        "chat api is disabled",
        "google chat is turned off",
        "requires newly granted chat space creation access",
    )):
        return ReconciliationDecision(
            "manual_required", "chat_external_configuration_required", None,
            {**evidence, "safe_resume_blocked": True},
        )
    if step.get("read_only"):
        return ReconciliationDecision(
            "safe_to_retry", "read_only_operation", step_id, evidence,
        )

    allowed_tools = (step.get("input_data") or {}).get("allowed_tools", [])
    contract = write_contract_for(
        step.get("service"), step.get("operation"), allowed_tools,
    )
    if contract is None:
        return ReconciliationDecision(
            "manual_required", "write_contract_unavailable", None, evidence,
        )

    executions = _tool_executions(step)
    attempted = attempted_required_tools(contract, executions)
    evidence["attempted_required_tools"] = attempted
    if not attempted:
        historical_attempts = step.get("historical_tool_attempts") or []
        historical_writes = [
            item for item in historical_attempts
            if item.get("tool_name") in WRITE_TOOLS
        ]
        if historical_writes:
            evidence["historical_write_tools"] = [
                str(item.get("tool_name")) for item in historical_writes
            ]
            return ReconciliationDecision(
                "manual_required", "legacy_write_evidence_requires_reconciliation",
                None, evidence,
            )
        category = step.get("error_category") or run.get("error_category")
        if category == "tool_selection":
            return ReconciliationDecision(
                "safe_to_retry", "no_write_tool_attempted", step_id, evidence,
            )
        if category == "worker_reconciliation":
            return ReconciliationDecision(
                "manual_required", "write_acceptance_unknown", None, evidence,
            )
        return ReconciliationDecision(
            "safe_to_retry", "no_external_write_evidence", step_id, evidence,
        )

    outcome = await verify_executions_detailed(
        executions, service=step.get("service"), operation=step.get("operation"),
    )
    evidence["verification"] = outcome.evidence
    if outcome.passed:
        return ReconciliationDecision(
            "already_completed", "postconditions_already_satisfied", step_id, evidence,
        )

    attempted_writes = {
        item.get("tool") for item in executions if item.get("tool") in WRITE_TOOLS
    }
    explicit_failures = all(
        isinstance(item.get("result"), dict)
        and (
            item["result"].get("error")
            or item["result"].get("success") is False
        )
        for item in executions if item.get("tool") in attempted_writes
    )
    if (
        attempted_writes
        and attempted_writes.issubset(_INTRINSICALLY_IDEMPOTENT_WRITES)
        and explicit_failures
    ):
        return ReconciliationDecision(
            "safe_to_retry", "idempotent_write_explicitly_failed", step_id, evidence,
        )

    if "append_to_google_sheet" in attempted_writes:
        reason = "sheet_append_cannot_be_proven"
    elif outcome.boundary == "expected_state_mismatch":
        reason = "provider_state_conflicts_with_expected_state"
    else:
        reason = "external_write_outcome_uncertain"
    return ReconciliationDecision("manual_required", reason, None, evidence)
