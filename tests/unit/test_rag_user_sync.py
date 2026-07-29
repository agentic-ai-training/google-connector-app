from datetime import datetime, timezone

from app.rag.ingestion import _timestamp


def test_rag_ingestion_normalizes_provider_timestamp_for_asyncpg():
    value = _timestamp("2026-07-28T14:12:55-07:00")

    assert isinstance(value, datetime)
    assert value.utcoffset() is not None
    assert value.astimezone(timezone.utc).isoformat() == (
        "2026-07-28T21:12:55+00:00"
    )


def test_rag_ingestion_rejects_invalid_provider_timestamp():
    assert _timestamp("not-a-time") is None
