import asyncio
import time
import uuid

import pytest

from app.rag import retriever


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class _LexicalConnection:
    async def fetch(self, query, *arguments):
        assert "websearch_to_tsquery" in query
        assert arguments[0] == "Google evidence"
        return [{
            "id": uuid.uuid4(),
            "source": "gmail",
            "source_id": "message-1",
            "parent_id": None,
            "content": "Google Workspace historical evidence",
            "score": 0.8,
            "metadata": {},
            "source_modified_at": None,
            "chunker_version": "gmail-v3-parent",
        }]


@pytest.mark.asyncio
async def test_hybrid_retrieval_falls_back_to_keyword_with_bounded_embedding(monkeypatch):
    class SlowEmbedder:
        async def aembed_query(self, _query):
            await asyncio.sleep(1)

    monkeypatch.setattr(retriever, "NomicEmbedder", SlowEmbedder)
    diagnostics = {}
    started = time.perf_counter()

    results = await retriever.hybrid_retrieve(
        "Find conceptually related historical documents about Google evidence",
        pool=_Pool(_LexicalConnection()),
        user_id="tenant@example.com",
        diagnostics=diagnostics,
        embedding_timeout_seconds=0.1,
    )

    assert time.perf_counter() - started < 0.5
    assert len(results) == 1
    assert diagnostics["dense_status"] == "timeout"
    assert diagnostics["effective_mode"] == "keyword"
    assert diagnostics["lexical_candidates"] == 1
    assert diagnostics["lexical_query_strategy"] == "explicit_topic_tail"
    assert diagnostics["lexical_query_token_count"] == 2
