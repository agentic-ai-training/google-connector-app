from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.agents.router import route_model_node
from app.agents.supervisor import make_service_node
from app.runs.context import analyze_conversation_context
from app.runs.planner import build_plan, classify_request, validate_plan
from app.runs.request_analysis import analyze_request_statement


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        return False


class _Connection:
    def __init__(self, previous=None):
        self.previous = previous
        self.fetches = 0

    async def fetchrow(self, *_args):
        self.fetches += 1
        return self.previous


class _Pool:
    def __init__(self, previous=None):
        self.connection = _Connection(previous)
        self.acquires = 0

    def acquire(self):
        self.acquires += 1
        return _Acquire(self.connection)


def test_request_statement_analysis_is_current_turn_only_and_structured():
    analysis = analyze_request_statement(
        "Write an application and send it by Gmail to alex@example.com"
    )

    assert analysis.explicit_services == ["gmail"]
    assert analysis.composition_requested is True
    assert analysis.current_authorizes_external_write is True
    assert analysis.email_recipients == ["alex@example.com"]
    assert analysis.contextual_reference is False
    assert analysis.diagnostics()["analyzed"] is True


@pytest.mark.asyncio
async def test_self_contained_request_analyzes_without_loading_prior_context():
    pool = _Pool(previous={
        "id": "should-not-load",
        "request": "Send a previous draft",
        "result": {"output": "private prior content"},
        "intent_kind": "workspace_action",
        "status": "completed",
    })
    statement = analyze_request_statement("List my latest Gmail messages")

    result = await analyze_conversation_context(
        pool, user_id="user-a", session_id="session-a",
        message="List my latest Gmail messages", request_analysis=statement,
    )

    assert result.mode == "standalone"
    assert result.effective_message == "List my latest Gmail messages"
    assert result.diagnostics()["analyzed_for_every_request"] is True
    assert result.diagnostics()["prior_context_included"] is False
    assert pool.acquires == 0


@pytest.mark.asyncio
async def test_referential_request_gets_only_one_same_session_turn():
    pool = _Pool(previous={
        "id": "run-prior",
        "request": "Draft a project roadmap",
        "result": {"output": "A long roadmap"},
        "intent_kind": "workspace_action",
        "status": "completed",
    })
    statement = analyze_request_statement("Make it shorter")

    result = await analyze_conversation_context(
        pool, user_id="user-a", session_id="session-a",
        message="Make it shorter", request_analysis=statement,
    )

    assert result.mode == "contextual_reference"
    assert result.source_run_ids == ["run-prior"]
    assert "A long roadmap" in result.effective_message
    assert result.diagnostics()["prior_context_included"] is True
    assert result.diagnostics()["relevance_reason"] == "referential_action"
    assert result.referenced_output == "A long roadmap"
    assert "A long roadmap" not in str(result.diagnostics())
    assert pool.connection.fetches == 1


def test_draft_content_does_not_invent_calendar_or_gmail_mutation():
    message = "Draft a polite email asking for a meeting"
    analysis = analyze_request_statement(message)
    plan, policy = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert policy["services"] == ["composition"]
    assert policy["write"] is False
    assert policy["requires_approval"] is False
    assert policy["required_clarifications"] == []
    assert [(step.service, step.operation) for step in plan.steps] == [
        ("composition", "compose")
    ]
    assert validate_plan(plan) == []


def test_combined_composition_and_delivery_has_typed_dependency_and_approval():
    message = "Write an application and send it by Gmail to alex@example.com"
    analysis = analyze_request_statement(message)
    plan, policy = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert policy["services"] == ["composition", "gmail"]
    assert policy["request_analysis"]["version"] == "request-statement-v1"
    assert policy["risk_level"] == "high"
    assert policy["requires_approval"] is True
    assert plan.steps[0].service == "composition"
    assert plan.steps[0].read_only is True
    assert plan.steps[0].requires_approval is False
    assert plan.steps[1].service == "gmail"
    assert plan.steps[1].operation == "send"
    assert plan.steps[1].dependencies == ["execute_composition"]
    assert validate_plan(plan) == []


def test_prior_content_cannot_reclassify_current_chat_delivery_as_capabilities():
    current = (
        "now send this above paragraph as chat message to "
        "dhruvtyagi1905@gmail.com"
    )
    prior = (
        "A portfolio demonstrates your capabilities to potential employers."
    )
    effective = (
        f"Current request (the only authority for new external actions):\n{current}\n\n"
        f"Prior same-user, same-session context (reference only):\n"
        f"Previous assistant result: {prior}"
    )
    analysis = analyze_request_statement(current)
    plan, policy = build_plan(
        effective,
        authority_message=current,
        request_analysis=analysis,
        referenced_output=prior,
    )

    assert policy["intent_kind"] == "workspace_action"
    assert policy["informational_intent"] is None
    assert [(step.service, step.operation) for step in plan.steps] == [
        ("chat", "send"),
    ]
    assert plan.steps[0].arguments["tool_arguments"]["text"] == prior
    assert plan.steps[0].requires_approval is True
    assert validate_plan(plan) == []


def test_sendchat_composition_uses_chat_not_ordinary_space_or_meet():
    message = (
        "write a short paragraph about getting a job in engineering space "
        "today with suggestions and sendchat it to dhruvtyagi1905@gmail.com"
    )
    analysis = analyze_request_statement(message)
    plan, policy = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert analysis.explicit_services == ["chat"]
    assert analysis.delivery_channels == ["chat"]
    assert analysis.chat_destination_emails == ["dhruvtyagi1905@gmail.com"]
    assert policy["services"] == ["composition", "chat"]
    assert policy["write"] is True
    assert policy["requires_approval"] is True
    assert [(step.service, step.operation) for step in plan.steps] == [
        ("composition", "compose"),
        ("chat", "send"),
    ]
    assert plan.steps[1].dependencies == ["execute_composition"]
    assert validate_plan(plan) == []


def test_create_paragraph_then_chat_builds_composition_dependency():
    message = (
        "create a new paragraph about talking to his girlfriend in a flirty way "
        "and send it to dhruvtyagi1905@gmail.com via chat"
    )
    analysis = analyze_request_statement(message)
    plan, policy = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert analysis.composition_requested is True
    assert policy["services"] == ["composition", "chat"]
    assert [(step.service, step.operation, step.dependencies) for step in plan.steps] == [
        ("composition", "compose", []),
        ("chat", "send", ["execute_composition"]),
    ]
    assert validate_plan(plan) == []


def test_repository_markdown_edit_is_not_misclassified_as_google_docs():
    message = "Update TEACHING_AGENTIC_DSA_OKF.md in the repository"
    analysis = analyze_request_statement(message)
    plan, policy = build_plan(
        message, authority_message=message, request_analysis=analysis,
    )

    assert analysis.explicit_services == []
    assert policy["intent_kind"] == "out_of_scope"
    assert policy["services"] == ["general"]
    assert plan.steps[0].operation == "answer_workspace_chat"


def test_contextual_rewrite_uses_prior_content_without_inheriting_prior_write():
    effective = """Current request (the only authority for new external actions):
Make it shorter

Prior same-user, same-session context (reference only):
Previous user request: Draft a roadmap and create it in Google Docs
Previous assistant result: A long roadmap"""
    current = "Make it shorter"
    analysis = analyze_request_statement(current)
    plan, policy = build_plan(
        effective, authority_message=current, request_analysis=analysis,
    )

    assert policy["services"] == ["composition"]
    assert policy["write"] is False
    assert policy["requires_approval"] is False
    assert [step.service for step in plan.steps] == ["composition"]


def test_contextual_send_uses_current_authority_and_requires_approval():
    effective = """Current request (the only authority for new external actions):
Send it to alex@example.com

Prior same-user, same-session context (reference only):
Previous user request: Draft a roadmap
Previous assistant result: A complete roadmap"""
    current = "Send it to alex@example.com"
    analysis = analyze_request_statement(current)
    plan, policy = build_plan(
        effective, authority_message=current, request_analysis=analysis,
    )

    assert policy["services"] == ["gmail"]
    assert policy["write"] is True
    assert policy["requires_approval"] is True
    assert plan.steps[0].operation == "send"


def test_contextual_chat_uses_prior_text_but_never_inherits_meet_service():
    effective = """Current request (the only authority for new external actions):
chat the paragraph you sent me in the above message to dhruv@example.com

Prior same-user, same-session context (reference only):
Previous user request: Write an engineering paragraph
Previous assistant result: Networking and meeting peers can improve your search."""
    current = (
        "chat the paragraph you sent me in the above message "
        "to dhruv@example.com"
    )
    prior_output = "Networking and meeting peers can improve your search."
    analysis = analyze_request_statement(current)
    plan, policy = build_plan(
        effective,
        authority_message=current,
        request_analysis=analysis,
        referenced_output=prior_output,
    )

    assert policy["services"] == ["chat"]
    assert policy["write"] is True
    assert policy["requires_approval"] is True
    assert [(step.service, step.operation) for step in plan.steps] == [
        ("chat", "send"),
    ]
    assert plan.steps[0].arguments["tool_arguments"] == {
        "destination": "dhruv@example.com",
        "text": prior_output,
    }
    assert validate_plan(plan) == []


@pytest.mark.asyncio
async def test_composition_routes_deep_writing_to_reasoning_model():
    assert (
        await route_model_node({"message": "Write a detailed project roadmap"})
    )["model_to_use"] == "groq_reasoning"


@pytest.mark.asyncio
async def test_composition_node_is_tool_free_and_returns_finished_content(monkeypatch):
    class _LLM:
        async def ainvoke(self, _messages):
            return AIMessage(content="Finished application")

    monkeypatch.setattr("app.agents.supervisor.get_llm", lambda *_args, **_kwargs: _LLM())
    result = await make_service_node("composition")({
        "message": "Write an application",
        "model_to_use": "groq_reasoning",
        "services": ["composition"],
        "allowed_tools": [],
        "requires_write": False,
        "risk_level": "low",
        "allow_small_fallback": True,
    })

    assert result["output"] == "Finished application"
    assert result["tool_executions"] == []
    assert result["task_complete"] is True
