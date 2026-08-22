from types import SimpleNamespace

import pytest

from app.improvements import publisher


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"token": "short-lived-installation-token"}


class _Client:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


@pytest.mark.asyncio
async def test_github_app_mints_scoped_installation_token(monkeypatch):
    settings = SimpleNamespace(
        github_coding_app_id="1234",
        github_coding_app_installation_id="5678",
        github_coding_app_private_key="private-key",
        github_proposal_token="legacy-token",
    )
    monkeypatch.setattr(publisher, "get_settings", lambda: settings)
    monkeypatch.setattr(publisher.jwt, "encode", lambda *args, **kwargs: "signed-app-jwt")
    client = _Client()

    headers = await publisher._github_api_headers(
        client, "agentic-ai-training/google-connector-app"
    )

    assert headers["Authorization"] == "Bearer short-lived-installation-token"
    assert client.calls[0][0].endswith("/app/installations/5678/access_tokens")
    assert client.calls[0][1]["json"] == {"repositories": ["google-connector-app"]}
    assert client.calls[0][1]["headers"]["Authorization"] == "Bearer signed-app-jwt"


@pytest.mark.asyncio
async def test_partial_github_app_configuration_fails_closed(monkeypatch):
    settings = SimpleNamespace(
        github_coding_app_id="1234",
        github_coding_app_installation_id="",
        github_coding_app_private_key="",
        github_proposal_token="legacy-token",
    )
    monkeypatch.setattr(publisher, "get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="must be configured together"):
        await publisher._github_api_headers(
            _Client(), "agentic-ai-training/google-connector-app"
        )


@pytest.mark.asyncio
async def test_legacy_token_is_only_used_when_app_configuration_is_absent(monkeypatch):
    settings = SimpleNamespace(
        github_coding_app_id="",
        github_coding_app_installation_id="",
        github_coding_app_private_key="",
        github_proposal_token="legacy-token",
    )
    monkeypatch.setattr(publisher, "get_settings", lambda: settings)

    headers = await publisher._github_api_headers(
        _Client(), "agentic-ai-training/google-connector-app"
    )

    assert headers["Authorization"] == "Bearer legacy-token"
