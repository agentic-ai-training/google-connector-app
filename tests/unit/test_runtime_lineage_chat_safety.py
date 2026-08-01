import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from datetime import datetime, timedelta

from googleapiclient.errors import HttpError

from app.agents.supervisor import make_service_node, safe_write_failure_message
from app.runs.incident import build_incident, completion_from_steps
from app.runs.planner import (
    build_plan,
    calendar_create_arguments,
    validate_plan,
)
from app.runs.reconciliation import reconcile_failed_step
from app.runs.request_analysis import analyze_request_statement
from app.runs.worker import (
    _composition_output_error,
    _create_deterministic_calendar_event,
    _create_recent_senders_sheet,
    _chat_message_content,
    _dependency_context,
    _reconcile_verified_failed_siblings,
    _remaining_unresolved_steps,
    _send_deterministic_chat_message,
    _send_deterministic_gmail_copy,
    verify_step,
    _verified_terminal_output,
    _verified_sheet_url,
)
from app.tools.calendar_normalization import (
    normalize_calendar_datetime, normalize_calendar_window, normalize_timezone,
)
from app.tools.contracts import bind_ordered_output_lineage, write_contract_for
from app.tools.registry import _resolve_chat_space


@pytest.fixture(autouse=True)
def _isolate_worker_events(monkeypatch):
    async def append_event(*_args, **_kwargs):
        return 1

    monkeypatch.setattr("app.runs.worker.append_event", append_event)


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


def test_same_turn_gmail_copy_is_two_typed_steps_not_previous_context():
    message = (
        "fetch the last mail you sent to achintyat256@gmail.com, and send "
        "the same mail to dhruvvtyagi1905@gmail.com"
    )
    analysis = analyze_request_statement(message)
    plan, policy = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert analysis.contextual_reference is False
    assert policy["rag_mode"] == "none"
    assert [
        (step.id, step.operation, step.dependencies)
        for step in plan.steps
    ] == [
        ("execute_gmail_search", "search", []),
        ("execute_gmail_send", "send", ["execute_gmail_search"]),
    ]
    assert plan.steps[0].arguments["tool_arguments"] == {
        "query": "to:achintyat256@gmail.com in:sent",
        "max_results": 1,
    }
    assert plan.steps[1].arguments["tool_arguments"] == {
        "to": "dhruvvtyagi1905@gmail.com",
    }
    assert plan.steps[1].requires_approval is True
    assert validate_plan(plan) == []


def test_send_last_sent_mail_verb_order_still_builds_typed_copy_dag():
    message = (
        "send the last mail you sent to achintyat256@gmail.com, send it to , "
        "dhruvtyagi19@gmail.com"
    )
    analysis = analyze_request_statement(message)
    plan, policy = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert analysis.gmail_copy_requested is True
    assert analysis.contextual_reference is False
    assert policy["rag_mode"] == "none"
    assert [
        (step.id, step.operation, step.dependencies)
        for step in plan.steps
    ] == [
        ("execute_gmail_search", "search", []),
        ("execute_gmail_send", "send", ["execute_gmail_search"]),
    ]
    assert plan.steps[0].arguments["tool_arguments"] == {
        "query": "to:achintyat256@gmail.com in:sent",
        "max_results": 1,
    }
    assert plan.steps[1].arguments["tool_arguments"] == {
        "to": "dhruvtyagi19@gmail.com",
    }
    assert plan.steps[0].arguments["workflow_hints"]["copy_gmail_dependency"] is True
    assert plan.steps[1].arguments["workflow_hints"]["copy_gmail_dependency"] is True
    assert validate_plan(plan) == []


def test_live_read_refusal_without_tool_evidence_fails_postcondition():
    step = {
        "read_only": True,
        "input_data": {
            "allowed_tools": ["search_gmail", "get_gmail_message"],
        },
    }
    verified, evidence, artifacts = verify_step(step, {
        "task_complete": True,
        "output": "I'm sorry, but I can't fulfill that request.",
        "tool_results": [],
    })

    assert verified is False
    assert "refusal" in evidence
    assert artifacts == []


def test_live_read_requires_tool_evidence_but_composition_does_not():
    live_verified, evidence, _ = verify_step({
        "read_only": True,
        "input_data": {"allowed_tools": ["search_gmail"]},
    }, {
        "task_complete": True,
        "output": "Here is the result.",
        "tool_results": [],
    })
    composition_verified, _, _ = verify_step({
        "read_only": True,
        "input_data": {"allowed_tools": []},
    }, {
        "task_complete": True,
        "output": "A complete composed paragraph with no Google API claim.",
        "tool_results": [],
    })

    assert live_verified is False
    assert "without calling an allowed tool" in evidence
    assert composition_verified is True


@pytest.mark.asyncio
async def test_gmail_copy_binds_dependency_subject_and_body_without_model(
    monkeypatch,
):
    calls = []

    class _Envelope:
        compact_result = {"id": "sent-1"}

        @staticmethod
        def metadata():
            return {"truncated": False}

    async def execute(_tool, call, _state, _pool):
        calls.append(call)
        return None, {"id": "sent-1"}, _Envelope()

    monkeypatch.setattr(
        "app.runs.worker.get_toolsets",
        lambda: {"gmail": [SimpleNamespace(name="send_gmail")]},
    )
    monkeypatch.setattr("app.runs.worker.execute_tool_call", execute)
    result = await _send_deterministic_gmail_copy(
        object(),
        {"id": "run-1", "user_id": "user-1", "session_id": "session-1"},
        {
            "id": "step-2",
            "input_data": {
                "workflow_hints": {"copy_gmail_dependency": True},
                "tool_arguments": {"to": "target@example.com"},
            },
        },
        [{
            "service": "gmail",
            "output_data": {
                "tool_executions": [{
                    "tool": "get_gmail_message",
                    "result": {
                        "id": "source-1",
                        "subject": "Exact subject",
                        "body_excerpt": "Exact body",
                    },
                    "projection": {"truncated": False},
                }],
            },
        }],
    )

    assert result["task_complete"] is True
    assert calls[0]["name"] == "send_gmail"
    assert calls[0]["args"] == {
        "to": "target@example.com",
        "subject": "Exact subject",
        "body": "Exact body",
    }
    assert result["tool_executions"][0]["projection"]["lineage_bound"] is True


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


def test_verified_sheet_url_reads_completed_dependency_evidence():
    assert _verified_sheet_url([{
        "service": "sheets",
        "output_data": {
            "tool_executions": [{
                "tool": "create_google_sheet",
                "result": {
                    "spreadsheetId": "sheet-1",
                    "spreadsheetUrl": (
                        "https://docs.google.com/spreadsheets/d/sheet-1/edit"
                    ),
                },
            }],
        },
    }]) == "https://docs.google.com/spreadsheets/d/sheet-1/edit"


def test_chat_content_prefers_explicit_reference_then_composition_dependency():
    dependencies = [{
        "service": "composition",
        "output_data": {"output": "Freshly composed paragraph"},
    }]
    assert _chat_message_content(
        {"input_data": {"tool_arguments": {"text": "Referenced paragraph"}}},
        dependencies,
    ) == ("Referenced paragraph", "referenced_prior_assistant_output")
    assert _chat_message_content(
        {"input_data": {"tool_arguments": {}}},
        dependencies,
    ) == ("Freshly composed paragraph", "completed_composition_dependency")


def test_composition_postcondition_rejects_short_non_paragraph():
    request = "Create a new paragraph in a flirty tone"
    assert _composition_output_error(request, "Hey love, what's up?")
    assert _composition_output_error(
        request,
        (
            "I keep catching myself smiling whenever your name appears, and I "
            "think you should know that talking with you is my favourite distraction."
        ),
    ) is None


def test_verified_write_never_retains_pending_verification_message():
    pending = (
        "Required Google Workspace write operations completed; "
        "read-after-write verification is pending."
    )
    assert _verified_terminal_output({"service": "chat"}, pending, {}) == (
        "Sent the requested content in Google Chat and verified the message."
    )


def test_verified_gmail_receipt_names_recipient_body_subject_and_message():
    receipt = _verified_terminal_output(
        {"service": "gmail"},
        "Completed and verified the requested Gmail write.",
        {
            "tool_executions": [{
                "tool": "send_gmail",
                "arguments": {
                    "to": "person@example.com",
                    "subject": "Engineering role",
                    "body": "This is the exact paragraph that was sent.",
                },
                "result": {"id": "gmail-message-1"},
            }],
        },
    )

    assert "person@example.com" in receipt
    assert "Subject: Engineering role" in receipt
    assert "This is the exact paragraph that was sent." in receipt
    assert "Gmail message ID: gmail-message-1" in receipt


@pytest.mark.asyncio
async def test_complete_chat_workflow_resolves_then_sends_verified_sheet(
    monkeypatch,
):
    calls = []

    @tool(description="Resolve DM")
    def resolve_chat_destination(destination: str):
        return {}

    @tool(description="Send Chat")
    def send_chat_message(space_id: str, text: str):
        return {}

    class _Envelope:
        def __init__(self, result):
            self.compact_result = result

        def metadata(self):
            return {"truncated": False}

    async def execute(_tool, call, _state, _pool):
        calls.append(call)
        result = (
            {"name": "spaces/dm-1"}
            if call["name"] == "resolve_chat_destination"
            else {"name": "spaces/dm-1/messages/message-1"}
        )
        return None, result, _Envelope(result)

    monkeypatch.setattr(
        "app.runs.worker.get_toolsets",
        lambda: {"chat": [resolve_chat_destination, send_chat_message]},
    )
    monkeypatch.setattr("app.runs.worker.execute_tool_call", execute)
    result = await _send_deterministic_chat_message(
        object(),
        {"id": "run-1", "session_id": "s", "user_id": "u"},
        {
            "id": "chat-step",
            "input_data": {
                "tool_arguments": {"destination": "person@example.com"},
            },
        },
        [{
            "service": "sheets",
            "output_data": {
                "tool_executions": [{
                    "tool": "create_google_sheet",
                    "result": {
                        "spreadsheetId": "sheet-1",
                        "spreadsheetUrl": (
                            "https://docs.google.com/spreadsheets/d/sheet-1/edit"
                        ),
                    },
                }],
            },
        }],
    )
    assert [call["name"] for call in calls] == [
        "resolve_chat_destination", "send_chat_message",
    ]
    assert calls[1]["args"] == {
        "space_id": "spaces/dm-1",
        "text": "https://docs.google.com/spreadsheets/d/sheet-1/edit",
    }
    assert result["task_complete"] is True
    assert result["tool_executions"][1]["projection"]["lineage_bound"] is True


@pytest.mark.asyncio
async def test_complete_chat_workflow_sends_composition_dependency_exactly(
    monkeypatch,
):
    calls = []

    @tool(description="Resolve DM")
    def resolve_chat_destination(destination: str):
        return {}

    @tool(description="Send Chat")
    def send_chat_message(space_id: str, text: str):
        return {}

    class _Envelope:
        def __init__(self, result):
            self.compact_result = result

        def metadata(self):
            return {"truncated": False}

    async def execute(_tool, call, _state, _pool):
        calls.append(call)
        result = (
            {"name": "spaces/dm-1"}
            if call["name"] == "resolve_chat_destination"
            else {"name": "spaces/dm-1/messages/message-1"}
        )
        return None, result, _Envelope(result)

    monkeypatch.setattr(
        "app.runs.worker.get_toolsets",
        lambda: {"chat": [resolve_chat_destination, send_chat_message]},
    )
    monkeypatch.setattr("app.runs.worker.execute_tool_call", execute)
    result = await _send_deterministic_chat_message(
        object(),
        {"id": "run-1", "session_id": "s", "user_id": "u"},
        {
            "id": "chat-step",
            "input_data": {
                "tool_arguments": {"destination": "person@example.com"},
            },
        },
        [{
            "service": "composition",
            "output_data": {"output": "Exact composed paragraph"},
        }],
    )

    assert calls[1]["args"]["text"] == "Exact composed paragraph"
    assert (
        result["tool_executions"][1]["projection"]["content_source"]
        == "completed_composition_dependency"
    )
    assert result["output"] == "Sent the requested content in Google Chat."
    receipt = _verified_terminal_output(
        {"service": "chat"}, result["output"], result,
    )
    assert "person@example.com" in receipt
    assert "Exact composed paragraph" in receipt
    assert "spaces/dm-1/messages/message-1" in receipt


@pytest.mark.asyncio
async def test_chat_resolver_failure_never_attempts_send(monkeypatch):
    calls = []

    @tool(description="Resolve DM")
    def resolve_chat_destination(destination: str):
        return {}

    @tool(description="Send Chat")
    def send_chat_message(space_id: str, text: str):
        return {}

    class _Envelope:
        compact_result = {"error": "not found"}

        def metadata(self):
            return {"truncated": False}

    async def execute(_tool, call, _state, _pool):
        calls.append(call)
        return None, {"error": "not found"}, _Envelope()

    monkeypatch.setattr(
        "app.runs.worker.get_toolsets",
        lambda: {"chat": [resolve_chat_destination, send_chat_message]},
    )
    monkeypatch.setattr("app.runs.worker.execute_tool_call", execute)
    result = await _send_deterministic_chat_message(
        object(),
        {"id": "run-1", "session_id": "s", "user_id": "u"},
        {
            "id": "chat-step",
            "input_data": {
                "tool_arguments": {"destination": "person@example.com"},
            },
        },
        [{
            "service": "sheets",
            "output_data": {
                "output": (
                    "https://docs.google.com/spreadsheets/d/sheet-1/edit"
                ),
            },
        }],
    )
    assert [call["name"] for call in calls] == [
        "resolve_chat_destination",
    ]
    assert result["task_complete"] is False
    assert result["error_evidence"]["message_attempted"] is False


def test_calendar_clarifications_produce_deterministic_create_arguments():
    arguments = calendar_create_arguments(
        (
            "create a Meet invite tommorow 10 AM to person@example.com\n\n"
            "User clarifications:\n"
            "How long should the event last?: 1 minute\n"
            "Which timezone should be used?: Asia/Kolkata"
        ),
        "Asia/Kolkata",
        ["person@example.com"],
        add_meet=True,
    )

    assert arguments == {
        "title": "Meeting with person@example.com",
        "start_datetime": "tomorrow 10 AM",
        "duration_minutes": 1,
        "timezone": "Asia/Kolkata",
        "attendees": ["person@example.com"],
        "add_meet": True,
    }


def test_exact_multiservice_request_projects_chat_and_calendar_arguments():
    message = (
        "create a sheet of the names of last 20 people who did mails to me "
        "and google chat that drive link and a meet invite with a calender "
        "schedule of tommorow 10 AM to achintyat256@gmail.com\n\n"
        "User clarifications:\n"
        "How long should the event last?: 1 minute\n"
        "Which existing Google Chat space or direct-message email should receive "
        "the message?: achintyat256@gmail.com\n"
        "Which timezone should be used?: Asia/Kolkata"
    )
    analysis = analyze_request_statement(message)
    plan, _ = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )
    steps = {step.service: step for step in plan.steps}

    assert steps["chat"].arguments["tool_arguments"] == {
        "destination": "achintyat256@gmail.com",
    }
    assert steps["calendar"].arguments["tool_arguments"] == {
        "title": "Meeting with achintyat256@gmail.com",
        "start_datetime": "tomorrow 10 AM",
        "duration_minutes": 1,
        "timezone": "Asia/Kolkata",
        "attendees": ["achintyat256@gmail.com"],
        "add_meet": True,
    }


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


def test_chat_turned_off_is_account_configuration_not_project_api_failure():
    value = safe_write_failure_message("resolve_chat_destination", {
        "error": "HttpError 400: Google Chat is turned off for this account",
    })
    assert "signed-in Workspace account" in value
    assert "organization" in value
    assert "chat.googleapis.com" not in value


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


def test_chat_app_configuration_failure_is_actionable():
    value = safe_write_failure_message("resolve_chat_destination", {
        "error": (
            "HttpError 404: Google Chat app not found. "
            "To configure your Chat app, open the Configuration tab."
        ),
    })

    assert "enabled" in value.casefold()
    assert "configuration is incomplete" in value.casefold()
    assert "interactive features disabled" in value.casefold()


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
async def test_fully_specified_calendar_create_bypasses_model(monkeypatch):
    calls = []

    @tool(description="Create event")
    def create_calendar_event(
        title: str,
        start_datetime: str,
        end_datetime: str,
        attendees: list[str],
        add_meet: bool,
        timezone: str,
    ):
        return {}

    class _Envelope:
        compact_result = {
            "id": "event-1",
            "htmlLink": "https://calendar/event-1",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        }

        def metadata(self):
            return {"truncated": False}

    async def execute(_tool, call, _state, _pool):
        calls.append(call)
        return None, dict(_Envelope.compact_result), _Envelope()

    monkeypatch.setattr(
        "app.runs.worker.get_toolsets",
        lambda: {"calendar": [create_calendar_event]},
    )
    monkeypatch.setattr("app.runs.worker.execute_tool_call", execute)
    result = await _create_deterministic_calendar_event(
        object(),
        {"id": "run-1", "session_id": "s", "user_id": "u"},
        {
            "id": "calendar-step",
            "input_data": {
                "request": (
                    "create a Meet invite tommorow 10 AM to person@example.com\n\n"
                    "User clarifications:\n"
                    "How long should the event last?: 1 minute\n"
                    "Which timezone should be used?: Asia/Kolkata"
                ),
                "tool_arguments": {},
                "workflow_hints": {"add_meet_conference": True},
            },
        },
    )

    arguments = calls[0]["args"]
    assert datetime.fromisoformat(arguments["end_datetime"]) - datetime.fromisoformat(
        arguments["start_datetime"]
    ) == timedelta(minutes=1)
    assert arguments["attendees"] == ["person@example.com"]
    assert arguments["add_meet"] is True
    assert result["task_complete"] is True
    assert "Meet:" in result["output"]


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


@pytest.mark.asyncio
async def test_composition_dependency_projection_preserves_chat_content_lineage():
    class _Connection:
        async def fetch(self, *_args):
            return [{
                "step_key": "composition-1", "service": "composition",
                "output_data": {
                    "output": "Exact short paragraph for the recipient.",
                    "task_complete": True,
                },
            }]

    projected = await _dependency_context(_Connection(), {
        "run_id": "run-1", "dependencies": ["composition-1"],
    })

    assert _chat_message_content(
        {"input_data": {"tool_arguments": {}}},
        projected,
    ) == (
        "Exact short paragraph for the recipient.",
        "completed_composition_dependency",
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


@pytest.mark.asyncio
async def test_verified_failed_sibling_is_reconciled_without_retry(monkeypatch):
    step = {
        "id": "calendar-step",
        "run_id": "run-1",
        "service": "calendar",
        "operation": "create",
        "read_only": False,
        "status": "failed",
        "input_data": {"allowed_tools": ["create_calendar_event"]},
        "output_data": {"tool_executions": [{
            "tool": "create_calendar_event",
            "arguments": {"title": "Meeting"},
            "result": {"id": "event-1"},
        }]},
    }
    updates = []
    events = []

    class _Connection:
        async def fetch(self, query, *_args):
            if "FROM agent_run_steps" in query:
                return [step]
            if "FROM agent_artifacts" in query:
                return [{"external_id": "event-1"}]
            raise AssertionError(query)

        async def execute(self, query, *_args):
            updates.append(query)
            return "UPDATE 1"

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def reconciled(*_args, **_kwargs):
        return SimpleNamespace(
            state="already_completed",
            reason_code="postconditions_already_satisfied",
        )

    async def event(*args, **kwargs):
        events.append((args, kwargs))

    monkeypatch.setattr(
        "app.runs.worker.reconcile_failed_step", reconciled,
    )
    monkeypatch.setattr("app.runs.worker.append_event", event)

    count = await _reconcile_verified_failed_siblings(
        _Pool(), {"id": "run-1", "user_id": "user-1"},
    )

    assert count == 1
    assert len(updates) == 1
    assert events[0][0][3] == "step_reconciled"
    assert events[0][1]["payload"]["automatic_retry"] is False


def test_run_cannot_finalize_while_any_step_is_unresolved():
    steps = [
        {"id": "gmail", "status": "completed"},
        {"id": "calendar", "status": "failed"},
    ]

    assert _remaining_unresolved_steps(steps) == [steps[1]]
    assert _remaining_unresolved_steps([
        {"id": "gmail", "status": "completed"},
        {"id": "calendar", "status": "completed"},
    ]) == []


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
