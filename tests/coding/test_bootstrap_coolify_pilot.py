"""The operator bootstrap may create each named resource once, never a Service."""
import httpx
import pytest

from scripts.bootstrap_coolify_pilot import BootstrapError, bootstrap


@pytest.mark.asyncio
async def test_pilot_bootstrap_reads_then_creates_only_missing_resources():
    project = []
    environment = []
    posts = []

    def provider(request):
        path = request.url.path
        if path == "/api/v1/teams/current":
            return httpx.Response(200, json={"id": 0})
        if path == "/api/v1/projects":
            if request.method == "GET":
                return httpx.Response(200, json=project)
            posts.append(path)
            project.append({"name": "gca-generated-apps-pilot", "uuid": "project-one"})
            return httpx.Response(201, json={"uuid": "project-one"})
        if path == "/api/v1/projects/project-one/environments":
            if request.method == "GET":
                return httpx.Response(200, json=environment)
            posts.append(path)
            environment.append({"name": "pilot", "uuid": "environment-one"})
            return httpx.Response(201, json={"uuid": "environment-one"})
        if path == "/api/v1/projects/project-one/environment-one":
            return httpx.Response(200, json={"name": "pilot", "uuid": "environment-one"})
        raise AssertionError(f"unapproved API route: {path}")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18000", transport=httpx.MockTransport(provider),
    ) as client:
        assert await bootstrap(client, allow_create=False) == {
            "status": "missing_project", "created": False,
        }
        result = await bootstrap(client, allow_create=True)
        assert result == {"status": "verified", "project_uuid": "project-one",
                          "environment_uuid": "environment-one", "created": True}
        repeated = await bootstrap(client, allow_create=True)
    assert repeated["created"] is False
    assert posts == ["/api/v1/projects",
                     "/api/v1/projects/project-one/environments"]


@pytest.mark.asyncio
async def test_environment_without_uuid_resolves_by_name_without_recreating():
    def provider(request):
        path = request.url.path
        if path == "/api/v1/teams/current":
            return httpx.Response(200, json={"id": 0})
        if path == "/api/v1/projects":
            return httpx.Response(200, json=[{
                "name": "gca-generated-apps-pilot", "uuid": "project-one",
            }])
        if path == "/api/v1/projects/project-one/environments":
            return httpx.Response(200, json=[{"name": "pilot", "id": 7}])
        if path == "/api/v1/projects/project-one/pilot":
            return httpx.Response(200, json={"name": "pilot", "uuid": "env-from-detail"})
        if path == "/api/v1/projects/project-one/env-from-detail":
            return httpx.Response(200, json={"name": "pilot", "uuid": "env-from-detail"})
        raise AssertionError(f"unapproved API route: {path}")

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18000", transport=httpx.MockTransport(provider),
    ) as client:
        result = await bootstrap(client, allow_create=True)
    assert result["environment_uuid"] == "env-from-detail"
    assert result["created"] is False


@pytest.mark.asyncio
async def test_uncertain_create_and_duplicate_name_fail_without_second_post():
    posts = []

    def provider(request):
        path = request.url.path
        if path == "/api/v1/teams/current":
            return httpx.Response(200, json={"id": 0})
        if path == "/api/v1/projects" and request.method == "GET":
            return httpx.Response(200, json=[])
        posts.append(path)
        raise httpx.ReadTimeout("lost create response", request=request)

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18000", transport=httpx.MockTransport(provider),
    ) as client:
        with pytest.raises(BootstrapError, match="uncertain"):
            await bootstrap(client, allow_create=True)
    assert posts == ["/api/v1/projects"]

    def duplicate(request):
        if request.url.path == "/api/v1/teams/current":
            return httpx.Response(200, json={"id": 0})
        return httpx.Response(200, json=[
            {"name": "gca-generated-apps-pilot", "uuid": "one"},
            {"name": "gca-generated-apps-pilot", "uuid": "two"},
        ])

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18000", transport=httpx.MockTransport(duplicate),
    ) as client:
        with pytest.raises(BootstrapError, match="duplicate"):
            await bootstrap(client, allow_create=True)
