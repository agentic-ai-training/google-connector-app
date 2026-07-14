import pytest
from app.rag.context_packer import pack_context
from app.agents.router import route_model_node

def test_context_packer_orders_by_score():
    text = pack_context([
        {"source": "low", "content": "second", "score": 0.1},
        {"source": "high", "content": "first", "score": 0.9},
    ])
    assert text.index("first") < text.index("second")

@pytest.mark.asyncio
async def test_model_router():
    assert (await route_model_node({"message": "search gmail"}))["model_to_use"] == "groq"
    assert (await route_model_node({"message": "analyse and plan"}))["model_to_use"] == "deepseek"
