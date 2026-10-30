"""The pilot destination bootstrap cannot select the shared Coolify network."""
import httpx
import pytest

from scripts.bootstrap_coolify_pilot import BootstrapError
from scripts.bootstrap_coolify_pilot_destination import NETWORK, SERVER_UUID, bootstrap


@pytest.mark.asyncio
async def test_destination_bootstrap_creates_once_and_replays_read_only():
    destinations = []
    posts = []

    def provider(request):
        path = request.url.path
        if path == "/api/v1/teams/current":
            return httpx.Response(200, json={"id": 0})
        if path == "/api/v1/servers/" + SERVER_UUID:
            return httpx.Response(200, json={"uuid": SERVER_UUID})
        if path == "/api/v1/destinations":
            return httpx.Response(200, json=destinations)
        if path == "/api/v1/servers/" + SERVER_UUID + "/destinations":
            posts.append(path)
            payload = request.content.decode()
            assert NETWORK in payload and "coolify\"" not in payload
            destinations.append({
                "uuid": "pilot-destination", "network": NETWORK,
                "server_uuid": SERVER_UUID, "type": "standalone",
            })
            return httpx.Response(201, json=destinations[0])
        if path == "/api/v1/destinations/pilot-destination":
            return httpx.Response(200, json=destinations[0])
        raise AssertionError(f"unapproved API route: {path}")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18000", transport=httpx.MockTransport(provider),
    ) as client:
        assert await bootstrap(client, allow_create=False) == {
            "status": "missing_destination", "created": False,
        }
        first = await bootstrap(client, allow_create=True)
        repeat = await bootstrap(client, allow_create=True)
    assert first["created"] is True and first["network"] == NETWORK
    assert repeat["created"] is False and repeat["destination_uuid"] == "pilot-destination"
    assert posts == ["/api/v1/servers/" + SERVER_UUID + "/destinations"]


@pytest.mark.asyncio
async def test_destination_bootstrap_refuses_wrong_host_or_uncertain_post():
    posts = []
    existing = []

    def provider(request):
        path = request.url.path
        if path == "/api/v1/teams/current":
            return httpx.Response(200, json={"id": 0})
        if path == "/api/v1/servers/" + SERVER_UUID:
            return httpx.Response(200, json={"uuid": SERVER_UUID})
        if path == "/api/v1/destinations":
            return httpx.Response(200, json=existing)
        posts.append(path)
        raise httpx.ReadTimeout("lost response", request=request)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18000", transport=httpx.MockTransport(provider),
    ) as client:
        with pytest.raises(BootstrapError, match="uncertain"):
            await bootstrap(client, allow_create=True)
        existing.append({
            "uuid": "wrong-host", "network": NETWORK,
            "server_uuid": "other", "type": "standalone",
        })
        with pytest.raises(BootstrapError, match="approved host"):
            await bootstrap(client, allow_create=True)
    assert posts == ["/api/v1/servers/" + SERVER_UUID + "/destinations"]
