import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from app.rag.context_packer import pack_context
from app.agents.router import route_model_node
from app.agents.supervisor import make_service_node, supervisor_node
from app.api.middleware.auth import create_token
from jose import jwt
from app.config.settings import get_settings

def test_context_packer_orders_by_score():
    text = pack_context([
        {"source": "low", "content": "second", "score": 0.1},
        {"source": "high", "content": "first", "score": 0.9},
    ])
    assert text.index("first") < text.index("second")

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
