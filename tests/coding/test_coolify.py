import httpx
import pytest

from app.coding.coolify import CoolifyAPIError, CoolifyInventory, CoolifyTarget


def target(**changes):
    return CoolifyTarget(**{
        "target_id": "test-target", "base_url": "http://127.0.0.1:18000",
        "team_id": 0, "inventory_secret_ref": "local/coolify-inventory", **changes,
    })


@pytest.mark.parametrize("url", [
    "http://public.example", "https://user:pass@example.com", "https://example.com/api",
    "https://example.com?key=secret", "file:///tmp/secret", "https://example.com#fragment",
    "https://example.com:invalid",
])
def test_unsafe_origin_rejected_before_secret_lookup(url):
    with pytest.raises(ValueError):
        target(base_url=url)


@pytest.mark.asyncio
async def test_scope_checked_and_sensitive_payload_not_propagated():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/teams/current"):
            return httpx.Response(200, json={"id": 0})
        return httpx.Response(200, json=[{
            "uuid": "server-one", "private_key": "never-return",
            "settings": {"is_reachable": True, "is_usable": True, "secret": "hidden"},
        }])

    async with CoolifyInventory(target(), "test-token", transport=httpx.MockTransport(handler)) as client:
        assert await client.servers() == [{"uuid": "server-one", "reachable": True, "usable": True}]
    assert calls == ["/api/v1/teams/current", "/api/v1/servers"]


@pytest.mark.asyncio
async def test_wrong_team_stops_before_inventory():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": 99})

    async with CoolifyInventory(target(), "test-token", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoolifyAPIError, match="team_mismatch"):
            await client.servers()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,category", [(301, "provider_error"), (401, "authentication"),
                                          (403, "permission"), (429, "rate_limit")])
async def test_errors_redacted_and_redirects_never_followed(status, category):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(status, text="SECRET", headers={"Location": "https://attacker.example"})

    async with CoolifyInventory(target(), "test-token", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoolifyAPIError) as error:
            await client.verify_team()
        assert error.value.category == category
        assert "SECRET" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body,category", [(b"x" * 100, "response_too_large"), (b"{", "invalid_json")])
async def test_response_limits(body, category):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    async with CoolifyInventory(target(), "test-token", transport=transport) as client:
        client.MAX_BYTES = 50
        with pytest.raises(CoolifyAPIError) as error:
            await client.verify_team()
        assert error.value.category == category


@pytest.mark.asyncio
async def test_transport_timeout_has_no_sensitive_error_text():
    def handler(request):
        raise httpx.ReadTimeout("SECRET credentials in provider diagnostic", request=request)

    async with CoolifyInventory(target(), "test-token", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CoolifyAPIError) as error:
            await client.verify_team()
        assert str(error.value) == "coolify:timeout"


@pytest.mark.asyncio
async def test_unexpected_compression_rejected_before_body_expansion():
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b""),
    ))
    async with CoolifyInventory(target(), "test-token", transport=transport) as client:
        with pytest.raises(CoolifyAPIError, match="unsupported_compression"):
            await client.verify_team()
