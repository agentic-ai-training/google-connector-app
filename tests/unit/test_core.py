import pytest
import importlib
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from app.rag.context_packer import pack_context
from app.agents.router import route_model_node
from app.agents.supervisor import (
    make_service_node,
    recover_rejected_tool_call,
    supervisor_node,
)
from app.api.middleware.auth import create_token
from app.api.routes.chat import capability_answer, classify_graph_results
from app.db.google_clients import SCOPES
from app.db.oauth_credentials import missing_google_scopes
from jose import jwt
from app.config.settings import get_settings

def test_context_packer_orders_by_score():
    text = pack_context([
        {"source": "low", "content": "second", "score": 0.1},
        {"source": "high", "content": "first", "score": 0.9},
    ])
    assert text.index("first") < text.index("second")


@pytest.mark.parametrize(
    "service",
    ["gmail", "calendar", "drive", "docs", "sheets", "tasks", "chat", "contacts", "meet"],
)
def test_service_subgraph_module_exports_callable(service):
    module = importlib.import_module(f"app.agents.subagents.{service}_agent")
    node = getattr(module, f"{service}_subgraph")
    assert callable(node)
    assert node.__name__ == f"{service}_agent"


def test_graph_results_distinguish_retrieval_from_tool_execution():
    documents = [{"source": "gmail", "content": "Budget", "score": 0.9}]
    assert classify_graph_results({
        "retrieved_context": "Budget",
        "tool_results": documents,
    }) == (documents, None)
    assert classify_graph_results({
        "output": "Done",
        "tool_results": [{"message_id": "123"}],
    }) == (None, [{"message_id": "123"}])


def test_recover_rejected_groq_tool_call():
    error = RuntimeError(
        "tool_use_failed failed_generation="
        "<function=search_gmail{\"query\":\"budget meeting\",\"max_results\":10}"
        "</function>"
    )
    recovered = recover_rejected_tool_call(error)
    assert recovered is not None
    assert recovered.tool_calls[0]["name"] == "search_gmail"
    assert recovered.tool_calls[0]["args"] == {
        "query": "budget meeting",
        "max_results": 10,
    }

@pytest.mark.asyncio
async def test_model_router():
    assert (await route_model_node({"message": "search gmail"}))["model_to_use"] == "groq_fast"
    assert (await route_model_node({"message": "analyse and plan"}))["model_to_use"] == "groq_reasoning"


@pytest.mark.asyncio
async def test_supervisor_detects_multiple_services():
    result = await supervisor_node({"message": "email the document and create a task"})
    assert result["service"] == "gmail"
    assert result["services"] == ["gmail", "docs", "tasks"]


@pytest.mark.asyncio
async def test_service_node_executes_tool(monkeypatch):
    @tool(description="Echo a value")
    def echo(value: str):
        return {"echo": value}

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            assert tools == [echo]
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "echo", "args": {"value": "ok"},
                                 "id": "call-1", "type": "tool_call"}],
                )
            return AIMessage(content="verified")

    monkeypatch.setattr(
        "app.agents.supervisor.get_toolsets", lambda: {"gmail": [echo]}
    )
    monkeypatch.setattr("app.agents.supervisor.get_llm", lambda _: FakeLLM())
    result = await make_service_node("gmail")({
        "message": "echo",
        "model_to_use": "groq_fast",
        "services": ["gmail"],
        "session_id": "test",
    })
    assert result["output"] == "verified"
    assert result["tool_results"] == [{"echo": "ok"}]
    assert result["task_complete"] is True


@pytest.mark.asyncio
async def test_service_node_retries_rejected_groq_tool_generation(monkeypatch):
    @tool(description="Echo a value")
    def echo(value: str):
        return {"echo": value}

    class FlakyLLM:
        calls = 0

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("tool_use_failed")
            return AIMessage(content="recovered")

    monkeypatch.setattr(
        "app.agents.supervisor.get_toolsets", lambda: {"gmail": [echo]}
    )
    monkeypatch.setattr("app.agents.supervisor.get_llm", lambda _: FlakyLLM())
    result = await make_service_node("gmail")({
        "message": "echo",
        "model_to_use": "groq_fast",
        "services": ["gmail"],
        "session_id": "test",
    })
    assert result["output"] == "recovered"
    assert result["task_complete"] is True


def test_admin_claim_is_derived_from_email():
    settings = get_settings()
    admin = jwt.decode(
        create_token("achintyat256@gmail.com"),
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    user = jwt.decode(
        create_token("user@example.com"),
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    assert admin["admin"] is True
    assert user["admin"] is False


def test_capability_questions_are_answered_without_an_llm_call():
    answer = capability_answer("And other than Drive and Gmail, what about Meet?")
    assert answer is not None
    assert "Google Meet" in answer
    assert capability_answer("Search Gmail for invoices") is None


def test_added_google_scopes_require_fresh_consent():
    assert missing_google_scopes(SCOPES) == []
    without_meet = [scope for scope in SCOPES if "meetings.space" not in scope]
    missing = missing_google_scopes(without_meet)
    assert "https://www.googleapis.com/auth/meetings.space.created" in missing
    assert "https://www.googleapis.com/auth/meetings.space.readonly" in missing
