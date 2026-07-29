import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from datetime import datetime

from googleapiclient.errors import HttpError

from app.agents.supervisor import make_service_node, safe_write_failure_message
from app.runs.incident import build_incident, completion_from_steps
from app.runs.planner import build_plan, validate_plan
from app.runs.reconciliation import reconcile_failed_step
from app.runs.request_analysis import analyze_request_statement
from app.runs.worker import _create_recent_senders_sheet, _dependency_context
from app.tools.calendar_normalization import (
    normalize_calendar_datetime, normalize_calendar_window, normalize_timezone,
)
from app.tools.contracts import bind_ordered_output_lineage, write_contract_for
from app.tools.registry import _resolve_chat_space


class _Reply:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _HttpResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "Not Found"


def _http_error(status: int, message: str) -> HttpError:
    return HttpError(
        _HttpResponse(status),
        json.dumps({"error": {"message": message}}).encode(),
    )


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
    assert plan.steps[0].arguments["tool_arguments"] == {
        "destination": "achintyat256@gmail.com",
    }
    assert validate_plan(plan) == []


def test_calendar_natural_language_is_normalized_with_india_alias():
    assert normalize_timezone("India") == "Asia/Kolkata"
    now = datetime.fromisoformat("2026-07-29T02:00:00+05:30")
    assert normalize_calendar_datetime(
        "tomorrow 10:00 AM India", "Asia/Kolkata", now=now,
    ) == "2026-07-30T10:00:00+05:30"
    start, end, timezone = normalize_calendar_window(
        "2026-07-30T10:00:00+05:30",
        "2026-07-30T10:15:00+05:30",
        "Indian",
    )
    assert (start, end, timezone) == (
        "2026-07-30T10:00:00+05:30",
        "2026-07-30T10:15:00+05:30",
        "Asia/Kolkata",
    )


def test_chat_api_disabled_error_is_actionable_and_sanitized():
    value = safe_write_failure_message("send_chat_message", {
        "error": (
            "403 Google Chat API has not been used in project 351387763928 "
            "before or it is disabled. Enable it at a private console URL."
        ),
    })
    assert "chat.googleapis.com" in value
    assert "351387763928" not in value
    assert "console" not in value


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
        get=lambda **kwargs: _Reply({"name": kwargs["name"]}),
    )
    monkeypatch.setattr(
        "app.tools.registry.g.chat_service",
        SimpleNamespace(spaces=lambda: spaces),
    )

    assert _resolve_chat_space("person@example.com") == {
        "name": "spaces/direct-1",
        "created": False,
        "kind": "direct_message",
        "recipient": "users/person@example.com",
        "readbackVerified": True,
    }
    assert _resolve_chat_space("spaces/existing") == {
        "name": "spaces/existing",
        "created": False,
        "kind": "space_resource",
        "readbackVerified": True,
    }


def test_missing_chat_dm_is_idempotently_created(monkeypatch):
    setup_calls = []
    missing = _http_error(404, "The specified direct message doesn't exist.")

    def setup(*, body):
        setup_calls.append({"body": body})
        return _Reply({"name": "spaces/new-dm"})

    spaces = SimpleNamespace(
        findDirectMessage=lambda **_kwargs: _Reply(missing),
        setup=setup,
        get=lambda **kwargs: _Reply({"name": kwargs["name"]}),
    )
    monkeypatch.setattr(
        "app.tools.registry.g.chat_service",
        SimpleNamespace(spaces=lambda: spaces),
    )

    resolved = _resolve_chat_space("Person@Example.com")

    assert resolved == {
        "name": "spaces/new-dm",
        "created": True,
        "kind": "direct_message",
        "recipient": "users/person@example.com",
        "readbackVerified": True,
    }
    setup_body = setup_calls[0]["body"]
    assert setup_body["requestId"]
    assert {
        key: value for key, value in setup_body.items() if key != "requestId"
    } == {
        "space": {
            "spaceType": "DIRECT_MESSAGE",
            "singleUserBotDm": False,
        },
        "memberships": [{
            "member": {
                "name": "users/person@example.com",
                "type": "HUMAN",
            },
        }],
    }


def test_unrelated_chat_404_is_not_treated_as_missing_dm(monkeypatch):
    spaces = SimpleNamespace(
        findDirectMessage=lambda **_kwargs: _Reply(
            _http_error(404, "A different resource was not found.")
        ),
    )
    monkeypatch.setattr(
        "app.tools.registry.g.chat_service",
        SimpleNamespace(spaces=lambda: spaces),
    )

    with pytest.raises(HttpError):
        _resolve_chat_space("person@example.com")


def test_chat_hostname_alone_is_not_misclassified_as_disabled():
    value = safe_write_failure_message("resolve_chat_destination", {
        "error": (
            "404 https://chat.googleapis.com/v1/spaces:findDirectMessage "
            "The specified direct message doesn't exist."
        ),
    })
    assert "could not find or create" in value
    assert "disabled" not in value


def test_chat_scope_error_requires_reconnect():
    value = safe_write_failure_message("resolve_chat_destination", {
        "error": "403 insufficient authentication scopes",
    })
    assert "reconnect Google" in value
    assert "creation access" in value


def test_chat_send_binds_resolved_space_lineage():
    contract = write_contract_for(
        "chat", "send",
        ["resolve_chat_destination", "send_chat_message"],
    )
    call, evidence = bind_ordered_output_lineage(
        contract,
        {
            "name": "send_chat_message",
            "args": {"space_id": "wrong", "text": "hello"},
        },
        [{
            "tool": "resolve_chat_destination",
            "result": {"name": "spaces/direct-1", "created": True},
        }],
    )

    assert call["args"]["space_id"] == "spaces/direct-1"
    assert evidence["lineage_bound"] is True


@pytest.mark.asyncio
async def test_chat_contract_binds_trusted_recipient_and_resolved_space(monkeypatch):
    resolutions = []
    sends = []

    @tool(description="Resolve Chat destination")
    def resolve_chat_destination(destination: str):
        resolutions.append(destination)
        return {
            "name": "spaces/direct-1",
            "kind": "direct_message",
            "created": True,
            "readbackVerified": True,
        }

    @tool(description="Send Chat message")
    def send_chat_message(space_id: str, text: str):
        sends.append((space_id, text))
        return {
            "name": "spaces/direct-1/messages/message-1",
            "resolvedSpace": space_id,
            "text": text,
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
                        "name": "resolve_chat_destination",
                        "args": {"destination": "hallucinated@example.com"},
                        "id": "resolve", "type": "tool_call",
                    },
                    {
                        "name": "send_chat_message",
                        "args": {"space_id": "spaces/wrong", "text": "hello"},
                        "id": "send", "type": "tool_call",
                    },
                ])
            return AIMessage(content="Sent.")

    monkeypatch.setattr(
        "app.agents.supervisor.get_toolsets",
        lambda: {"chat": [resolve_chat_destination, send_chat_message]},
    )
    monkeypatch.setattr("app.agents.supervisor.get_llm", lambda _: _LLM())

    result = await make_service_node("chat")({
        "message": "send hello", "model_to_use": "groq_fast",
        "services": ["chat"],
        "allowed_tools": ["resolve_chat_destination", "send_chat_message"],
        "tool_arguments": {"destination": "approved@example.com"},
        "requires_write": True, "operation": "send",
        "expected_write_tools": [
            "resolve_chat_destination", "send_chat_message",
        ],
        "write_completion_mode": "ordered", "session_id": "test",
    })

    assert result["task_complete"] is True
    assert resolutions == ["approved@example.com"]
    assert sends == [("spaces/direct-1", "hello")]
    assert result["tool_executions"][0]["projection"][
        "trusted_argument_bound"
    ] is True
    assert result["tool_executions"][1]["projection"]["lineage_bound"] is True


@pytest.mark.asyncio
async def test_completed_write_contract_does_not_require_post_tool_model_call(
    monkeypatch,
):
    @tool(description="Create a Calendar event")
    def create_calendar_event(title: str):
        return {"id": "event-1", "summary": title}

    class _LLM:
        calls = 0

        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError(
                    "A completed write contract must not require another model call"
                )
            return AIMessage(content="", tool_calls=[{
                "name": "create_calendar_event",
                "args": {"title": "Verified meeting"},
                "id": "calendar-create",
                "type": "tool_call",
            }])

    llm = _LLM()
    monkeypatch.setattr(
        "app.agents.supervisor.get_toolsets",
        lambda: {"calendar": [create_calendar_event]},
    )
    monkeypatch.setattr("app.agents.supervisor.get_llm", lambda _: llm)

    result = await make_service_node("calendar")({
        "message": "create a meeting",
        "model_to_use": "groq_fast",
        "services": ["calendar"],
        "allowed_tools": ["create_calendar_event"],
        "requires_write": True,
        "operation": "create",
        "expected_write_tools": ["create_calendar_event"],
        "write_completion_mode": "all",
        "session_id": "test",
        "allow_small_fallback": False,
    })

    assert result["task_complete"] is True
    assert llm.calls == 1
    assert result["tool_executions"][0]["result"]["id"] == "event-1"


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
async def test_recent_sender_dependency_projection_preserves_structured_rows():
    class _Connection:
        async def fetch(self, *_args):
            return [{
                "step_key": "gmail-1", "service": "gmail",
                "output_data": {
                    "output": "15 senders",
                    "tool_executions": [{
                        "tool": "list_recent_gmail_senders",
                        "arguments": {"max_results": 20},
                        "result": {"senders": [
                            {
                                "sender_name": "Ada",
                                "sender_email": "ada@example.com",
                                "received_at": "2026-07-29T00:00:00Z",
                            },
                        ], "returned": 1},
                    }],
                },
            }]

    projected = await _dependency_context(_Connection(), {
        "run_id": "run-1", "dependencies": ["gmail-1"],
    })
    sender = projected[0]["output_data"]["tool_executions"][0]["result"][
        "senders"
    ][0]
    assert sender["sender_name"] == "Ada"
    assert sender["sender_email"] == "ada@example.com"
    assert projected[0]["projection"]["dependency_projection"] == (
        "gmail_recent_senders_v1"
    )


def test_explicit_failed_write_does_not_reduce_side_effect_integrity():
    completion = completion_from_steps([{
        "status": "failed", "read_only": False, "weight": 1,
        "error_category": "tool_failure",
        "output_data": {"tool_executions": [{
            "tool": "send_chat_message",
            "result": {"error": "403 service disabled"},
        }]},
    }])
    assert completion["side_effect_integrity"] == 100


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


@pytest.mark.asyncio
async def test_explicit_failed_chat_resolver_is_safe_to_resume():
    decision = await reconcile_failed_step(
        {"id": "run-1", "error_category": "tool_failure"},
        {
            "id": "chat-step",
            "service": "chat",
            "operation": "send",
            "read_only": False,
            "error_category": "tool_failure",
            "input_data": {
                "allowed_tools": [
                    "resolve_chat_destination",
                    "send_chat_message",
                ],
            },
            "output_data": {"tool_executions": [{
                "tool": "resolve_chat_destination",
                "arguments": {"destination": "person@example.com"},
                "result": {
                    "error": "Got an unexpected keyword argument requestId",
                    "tool": "resolve_chat_destination",
                },
            }]},
        },
    )

    assert decision.state == "safe_to_retry"
    assert decision.resume_step_id == "chat-step"
    assert decision.reason_code == "idempotent_write_explicitly_failed"


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
