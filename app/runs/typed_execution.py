"""Safe preflight selection for deterministic durable tool execution.

The selector is deliberately pure: it never calls Google or a model.  A caller
may execute an eligible decision exactly once, or use the bounded agent path
when the decision is bypassed.  Provider/tool failures are not represented as
bypasses because a post-attempt model fallback could duplicate external writes.
"""

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import ValidationError

from app.tools.contracts import write_contract_for


DecisionStatus = Literal["eligible", "bypass"]


@dataclass(frozen=True)
class TypedExecutionDecision:
    status: DecisionStatus
    reason_code: str
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None


def _validated_arguments(tool: BaseTool, arguments: dict) -> dict:
    schema = tool.args_schema
    if schema is None:
        if arguments:
            raise ValueError("tool_has_no_argument_schema")
        return {}
    validated = schema.model_validate(arguments)
    return validated.model_dump()


def decide_typed_execution(
    step: dict,
    tools: dict[str, BaseTool],
) -> TypedExecutionDecision:
    """Select an exact single-tool path only when its complete schema validates."""
    input_data = step.get("input_data") or {}
    allowed = list(input_data.get("allowed_tools") or [])
    supplied = input_data.get("tool_arguments")
    if not isinstance(supplied, dict):
        return TypedExecutionDecision("bypass", "typed_arguments_not_an_object")

    if step.get("read_only", True):
        candidates = [name for name in allowed if name in tools]
        if len(candidates) != 1:
            return TypedExecutionDecision(
                "bypass", "read_operation_not_single_tool",
            )
        tool_name = candidates[0]
    else:
        contract = write_contract_for(
            step.get("service"), step.get("operation"), allowed,
        )
        if contract is None:
            return TypedExecutionDecision("bypass", "write_contract_missing")
        if len(contract.required_tools) != 1:
            return TypedExecutionDecision(
                "bypass", "ordered_or_multi_tool_contract",
            )
        tool_name = contract.required_tools[0]
        if tool_name not in tools:
            return TypedExecutionDecision("bypass", "contract_tool_unavailable")

    try:
        arguments = _validated_arguments(tools[tool_name], supplied)
    except (ValidationError, ValueError, TypeError):
        return TypedExecutionDecision(
            "bypass", "typed_arguments_incomplete_or_invalid",
        )
    return TypedExecutionDecision(
        "eligible", "complete_schema_validated", tool_name, arguments,
    )

