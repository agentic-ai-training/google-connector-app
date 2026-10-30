from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from scripts import sync_grafana_dashboards as sync


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        "https://example.grafana.net/api/dashboards/db",
        code,
        "transient",
        headers,
        BytesIO(b'{"code":"Loading"}'),
    )


def test_grafana_sync_retries_loading_then_succeeds(monkeypatch):
    responses = [_http_error(503), _http_error(503, "0"), _Response()]
    delays = []

    def fake_open(_request, timeout):
        assert timeout == 30
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(sync, "urlopen", fake_open)
    response = sync.open_with_transient_retry(
        Request("https://example.grafana.net/api/dashboards/db"),
        sleeper=delays.append,
    )

    assert isinstance(response, _Response)
    assert delays == [1, 0]
    assert responses == []


def test_grafana_sync_does_not_retry_permission_failure(monkeypatch):
    calls = 0

    def fake_open(_request, timeout):
        nonlocal calls
        calls += 1
        raise _http_error(403)

    monkeypatch.setattr(sync, "urlopen", fake_open)
    with pytest.raises(HTTPError, match="HTTP Error 403"):
        sync.open_with_transient_retry(
            Request("https://example.grafana.net/api/dashboards/db"),
            sleeper=lambda _delay: None,
        )

    assert calls == 1
