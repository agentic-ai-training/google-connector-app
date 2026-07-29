"""Sanitized human-readable previews for high-risk run approvals."""

from typing import Any


_VISIBLE_FIELDS = (
    "destination", "to", "recipient", "recipients", "attendees", "email",
    "title", "subject", "start_datetime", "end_datetime", "timezone",
    "add_meet", "file_id", "document_id", "spreadsheet_id", "task_id",
    "space_id", "range", "role",
)
_SENSITIVE_CONTENT_FIELDS = ("body", "message", "content", "text", "values")


def _argument_preview(arguments: dict[str, Any]) -> dict[str, Any]:
    preview = {
        field: arguments[field]
        for field in _VISIBLE_FIELDS
        if field in arguments and arguments[field] not in (None, "", [], {})
    }
    for field in _SENSITIVE_CONTENT_FIELDS:
        value = arguments.get(field)
        if value in (None, "", [], {}):
            continue
        preview[f"{field}_present"] = True
        preview[f"{field}_size"] = len(str(value))
    return preview


def build_approval_summary(plan, objective: str, policy: dict) -> dict:
    actions = []
    for step in plan.steps:
        if not step.requires_approval:
            continue
        data = step.arguments or {}
        contract = data.get("write_contract") or {}
        arguments = data.get("tool_arguments") or {}
        actions.append({
            "service": step.service,
            "operation": step.operation,
            "tools": list(contract.get("required_tools") or []),
            "arguments": _argument_preview(arguments),
            "execution_policy": (
                "typed adapter when complete; otherwise bounded agent before any "
                "external attempt"
            ),
        })
    return {
        "objective": objective,
        "risk": policy["risk_level"],
        "services": policy["services"],
        "actions": actions,
        "approval_scope": "this immutable plan only",
    }

