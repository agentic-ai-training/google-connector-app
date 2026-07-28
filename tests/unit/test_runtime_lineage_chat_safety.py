from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from app.agents.supervisor import make_service_node
from app.runs.incident import build_incident, completion_from_steps
from app.runs.planner import build_plan, validate_plan
from app.runs.reconciliation import reconcile_failed_step
from app.runs.request_analysis import analyze_request_statement
from app.runs.worker import _create_recent_senders_sheet
from app.tools.registry import _resolve_chat_space


class _Reply:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def test_chat_email_is_recipient_not_gmail_service_or_calendar_context():
    message = "Can you send a chat message to achintyat256@gmail.com saying hi?"
    analysis = analyze_request_statement(message)
    plan, policy = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert analysis.explicit_services == ["chat"]
    assert analysis.delivery_channels == ["chat"]
    assert analysis.chat_destination_emails == ["achintyat256@gmail.com"]
    assert policy["services"] == ["chat"]
    assert policy["required_clarifications"] == []
    assert [(step.service, step.operation) for step in plan.steps] == [
        ("chat", "send"),
    ]
    assert plan.steps[0].arguments["semantic_authorization"] == {
        "authorized": True,
        "basis": "current_turn_explicit_service",
    }
    assert validate_plan(plan) == []


def test_explicit_email_delivery_still_routes_gmail():
    message = "Send an email to achintyat256@gmail.com saying hi"
    analysis = analyze_request_statement(message)
    plan, _ = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert analysis.explicit_services == ["gmail"]
    assert analysis.delivery_channels == ["gmail"]
    assert [(step.service, step.operation) for step in plan.steps] == [
        ("gmail", "send"),
    ]


def test_chat_direct_message_email_resolves_to_space(monkeypatch):
    spaces = SimpleNamespace(
        findDirectMessage=lambda **kwargs: _Reply({
            "name": "spaces/direct-1", "requested": kwargs["name"],
        }),
    )
    monkeypatch.setattr(
        "app.tools.registry.g.chat_service",
        SimpleNamespace(spaces=lambda: spaces),
    )

    assert _resolve_chat_space("person@example.com") == "spaces/direct-1"
    assert _resolve_chat_space("spaces/existing") == "spaces/existing"


@pytest.mark.asyncio
async def test_ordered_sheet_calls_bind_created_id_before_population(monkeypatch):
    writes = []

    @tool(description="Create Sheet")
    def create_google_sheet(title: str):
        return {"spreadsheetId": "sheet-1", "spreadsheetUrl": "https://sheet"}

    @tool(description="Write Sheet")
    def write_google_sheet(
        spreadsheet_id: str, range: str, values: list[list[str]],
    ):
        writes.append(spreadsheet_id)
        return {
            "spreadsheetId": spreadsheet_id, "updatedRows": len(values),
            "updatedRange": range,
        }

    class _LLM:
        calls = 0

        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[
                    {
                        "name": "create_google_sheet",
                        "args": {"title": "Recent senders"},
                        "id": "create", "type": "tool_call",
                    },
                    {
                        "name": "write_google_sheet",
                        "args": {
                            "spreadsheet_id": "Recent senders",
                            "range": "Sheet1!A1:A2",
                            "values": [["Name"], ["Ada"]],
                        },
                        "id": "write", "type": "tool_call",
                    },
                ])
            return AIMessage(content="Created and populated.")

    monkeypatch.setattr(
        "app.agents.supervisor.get_toolsets",
        lambda: {"sheets": [create_google_sheet, write_google_sheet]},
    )
    monkeypatch.setattr("app.agents.supervisor.get_llm", lambda _: _LLM())
    result = await make_service_node("sheets")({
        "message": "create and populate", "model_to_use": "groq_fast",
        "services": ["sheets"],
        "allowed_tools": ["create_google_sheet", "write_google_sheet"],
        "requires_write": True, "operation": "create_and_write",
        "expected_write_tools": ["create_google_sheet", "write_google_sheet"],
        "write_completion_mode": "ordered", "session_id": "test",
    })

    assert result["task_complete"] is True
    assert writes == ["sheet-1"]
    assert result["tool_executions"][1]["arguments"]["spreadsheet_id"] == "sheet-1"
    assert result["tool_executions"][1]["projection"]["lineage_bound"] is True


@pytest.mark.asyncio
async def test_partial_write_failure_preserves_successful_artifact_evidence(monkeypatch):
    @tool(description="Create Sheet")
    def create_google_sheet(title: str):
        return {"spreadsheetId": "sheet-1", "spreadsheetUrl": "https://sheet"}

    @tool(description="Write Sheet")
    def write_google_sheet(
        spreadsheet_id: str, range: str, values: list[list[str]],
    ):
        return {"error": "Requested entity was not found", "tool": "write_google_sheet"}

    class _LLM:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            return AIMessage(content="", tool_calls=[
                {
                    "name": "create_google_sheet",
                    "args": {"title": "Recent senders"},
                    "id": "create", "type": "tool_call",
                },
                {
                    "name": "write_google_sheet",
                    "args": {
                        "spreadsheet_id": "wrong",
                        "range": "Sheet1!A1:A2",
                        "values": [["Name"], ["Ada"]],
                    },
                    "id": "write", "type": "tool_call",
                },
            ])

    monkeypatch.setattr(
        "app.agents.supervisor.get_toolsets",
        lambda: {"sheets": [create_google_sheet, write_google_sheet]},
    )
    monkeypatch.setattr("app.agents.supervisor.get_llm", lambda _: _LLM())
    result = await make_service_node("sheets")({
        "message": "create and populate", "model_to_use": "groq_fast",
        "services": ["sheets"],
        "allowed_tools": ["create_google_sheet", "write_google_sheet"],
        "requires_write": True, "operation": "create_and_write",
        "expected_write_tools": ["create_google_sheet", "write_google_sheet"],
        "write_completion_mode": "ordered", "session_id": "test",
    })

    assert result["task_complete"] is False
    assert len(result["tool_executions"]) == 2
    assert result["tool_executions"][0]["result"]["spreadsheetId"] == "sheet-1"
    assert "Google Sheets could not find" in result["error"]
    completion = completion_from_steps([{
        "status": "failed", "read_only": False, "weight": 1,
        "error_category": "tool_failure",
        "output_data": {"tool_executions": result["tool_executions"]},
    }])
    assert completion["side_effect_integrity"] == 75


@pytest.mark.asyncio
async def test_recent_senders_sheet_is_deterministic_and_lineage_bound(monkeypatch):
    calls = []

    @tool(description="Create")
    def create_google_sheet(title: str):
        return {}

    @tool(description="Write")
    def write_google_sheet(
        spreadsheet_id: str, range: str, values: list[list[str]],
    ):
        return {}

    class _Envelope:
        def __init__(self, result):
            self.compact_result = result

        def metadata(self):
            return {"truncated": False}

    async def execute(_tool, call, _state, _pool):
        calls.append(call)
        if call["name"] == "create_google_sheet":
            result = {
                "spreadsheetId": "sheet-1",
                "spreadsheetUrl": "https://sheet",
            }
        else:
            result = {
                "spreadsheetId": call["args"]["spreadsheet_id"],
                "updatedRows": len(call["args"]["values"]),
            }
        return None, result, _Envelope(result)

    monkeypatch.setattr(
        "app.runs.worker.get_toolsets",
        lambda: {"sheets": [create_google_sheet, write_google_sheet]},
    )
    monkeypatch.setattr("app.runs.worker.execute_tool_call", execute)
    dependencies = [{
        "output_data": {"tool_executions": [{
            "tool": "list_recent_gmail_senders",
            "result": {"senders": [
                {"sender_name": "Ada", "sender_email": "ada@example.com"},
                {"sender_name": "", "sender_email": "grace@example.com"},
            ]},
        }]},
    }]
    result = await _create_recent_senders_sheet(
        object(),
        {"id": "run-12345678", "session_id": "s", "user_id": "u"},
        {"id": "step-1"}, dependencies,
    )

    assert result["task_complete"] is True
    assert calls[1]["args"]["spreadsheet_id"] == "sheet-1"
    assert calls[1]["args"]["values"] == [
        ["Name", "Email"],
        ["Ada", "ada@example.com"],
        ["grace@example.com", "grace@example.com"],
    ]
    assert result["tool_executions"][1]["projection"]["lineage_bound"] is True


@pytest.mark.asyncio
async def test_legacy_write_attempt_without_result_evidence_requires_reconciliation():
    decision = await reconcile_failed_step(
        {"id": "run-1", "error_category": "tool_failure"},
        {
            "id": "step-1", "service": "sheets",
            "operation": "create_and_write", "read_only": False,
            "error_category": "tool_failure",
            "input_data": {
                "allowed_tools": ["create_google_sheet", "write_google_sheet"],
            },
            "output_data": {"tool_executions": []},
            "historical_tool_attempts": [
                {"tool_name": "create_google_sheet", "status": "success"},
            ],
        },
    )

    assert decision.state == "manual_required"
    assert decision.reason_code == "legacy_write_evidence_requires_reconciliation"


def test_successful_unintended_write_reduces_side_effect_integrity():
    steps = [{
        "id": "step-1", "title": "Send Gmail message",
        "service": "gmail", "operation": "send",
        "status": "completed", "read_only": False, "weight": 1,
        "input_data": {
            "semantic_authorization": {
                "authorized": False,
                "basis": "no_current_turn_service_authority",
            },
        },
        "output_data": {"output": "provider accepted"},
    }]
    completion = completion_from_steps(steps)
    incident = build_incident(steps, "policy_violation", "Delivery mismatch")

    assert completion["technical_completion"] == 100
    assert completion["side_effect_integrity"] == 75
    assert incident["unintended_side_effects"] == [{
        "step_id": "step-1",
        "title": "Send Gmail message",
        "service": "gmail",
        "operation": "send",
        "verified": True,
    }]
    assert "did not match the user's requested delivery channel" in (
        incident["contributing_factors"][0]
    )
