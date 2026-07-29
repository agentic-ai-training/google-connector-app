from langchain_core.tools import tool

from app.runs.approval_preview import build_approval_summary
from app.runs.planner import ExecutionPlan, PlanStep
from app.runs.typed_execution import decide_typed_execution


@tool(description="Send an email")
def _send_gmail(to: str, subject: str, body: str) -> dict:
    return {"id": "message-1"}


@tool(description="Search Drive")
def _search_drive(query: str, max_results: int = 10) -> dict:
    return {"files": []}


def _step(*, read_only: bool, allowed: list[str], arguments: dict,
          service: str = "gmail", operation: str = "send") -> dict:
    return {
        "service": service,
        "operation": operation,
        "read_only": read_only,
        "input_data": {
            "allowed_tools": allowed,
            "tool_arguments": arguments,
        },
    }


def test_complete_single_write_contract_selects_typed_adapter():
    decision = decide_typed_execution(
        _step(
            read_only=False,
            allowed=["send_gmail"],
            arguments={
                "to": "person@example.com",
                "subject": "Hello",
                "body": "Hi",
            },
        ),
        {"send_gmail": _send_gmail},
    )
    assert decision.status == "eligible"
    assert decision.tool_name == "send_gmail"
    assert decision.arguments["to"] == "person@example.com"


def test_incomplete_write_arguments_select_guarded_pre_attempt_fallback():
    decision = decide_typed_execution(
        _step(
            read_only=False,
            allowed=["send_gmail"],
            arguments={"to": "person@example.com"},
        ),
        {"send_gmail": _send_gmail},
    )
    assert decision.status == "bypass"
    assert decision.reason_code == "typed_arguments_incomplete_or_invalid"


def test_exact_single_read_selects_typed_adapter():
    decision = decide_typed_execution(
        _step(
            read_only=True,
            allowed=["search_drive"],
            arguments={"query": "quarterly report"},
            service="drive",
            operation="search",
        ),
        {"search_drive": _search_drive},
    )
    assert decision.status == "eligible"
    assert decision.arguments == {"query": "quarterly report", "max_results": 10}


def test_ordered_contract_is_left_to_bounded_executor():
    decision = decide_typed_execution(
        _step(
            read_only=False,
            allowed=["resolve_chat_destination", "send_chat_message"],
            arguments={"destination": "person@example.com"},
            service="chat",
            operation="send",
        ),
        {},
    )
    assert decision.status == "bypass"
    assert decision.reason_code == "ordered_or_multi_tool_contract"


def test_approval_preview_shows_targets_but_not_message_contents():
    step = PlanStep(
        id="send",
        title="Send Gmail",
        service="gmail",
        operation="send",
        arguments={
            "tool_arguments": {
                "to": "person@example.com",
                "subject": "Update",
                "body": "private body",
            },
            "write_contract": {
                "required_tools": ["send_gmail"],
                "completion_mode": "all",
            },
        },
        read_only=False,
        risk_level="high",
        requires_approval=True,
        weight=1,
    )
    plan = ExecutionPlan(
        objective="Send the update",
        intent_kind="workspace_action",
        services=["gmail"],
        rag_mode="none",
        steps=[step],
        success_criteria=[],
        estimated_max_tokens=100,
    )
    preview = build_approval_summary(
        plan, plan.objective,
        {"risk_level": "high", "services": ["gmail"]},
    )
    action = preview["actions"][0]
    assert action["arguments"]["to"] == "person@example.com"
    assert action["arguments"]["body_present"] is True
    assert "body" not in action["arguments"]

