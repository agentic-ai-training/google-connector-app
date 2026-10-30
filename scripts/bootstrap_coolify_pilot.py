"""Operator-only, one-shot Coolify project/environment bootstrap over private IAP.

No model may call this script. It creates no Service, network, domain or VM. A
provider timeout after POST is uncertain and must be reconciled, never retried
automatically. The token value and provider bodies are not printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import stat

from dotenv import dotenv_values
import httpx


PROJECT_NAME = "gca-generated-apps-pilot"
ENVIRONMENT_NAME = "pilot"
UUID = re.compile(r"^[A-Za-z0-9-]{1,128}$")


class BootstrapError(RuntimeError):
    pass


def private_token(path: Path) -> str:
    if (path.is_symlink() or not path.is_file()
            or stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise BootstrapError("configuration token file is not private")
    token = dotenv_values(path).get("COOLIFY_CONFIGURATION_TOKEN")
    if not isinstance(token, str) or not token or any(char.isspace() for char in token):
        raise BootstrapError("configuration token is unavailable")
    return token


async def response(client: httpx.AsyncClient, method: str, path: str, *, body=None,
                   expected: int = 200, result_type=dict):
    try:
        async with client.stream(method, path, json=body) as result:
            if result.status_code != expected:
                raise BootstrapError(f"Coolify {method} returned HTTP {result.status_code}")
            if result.headers.get("content-encoding", "identity") != "identity":
                raise BootstrapError("unexpected provider encoding")
            data = bytearray()
            async for chunk in result.aiter_bytes():
                if len(data) + len(chunk) > 131_072:
                    raise BootstrapError("provider response too large")
                data.extend(chunk)
            parsed = json.loads(data)
            if not isinstance(parsed, result_type):
                raise BootstrapError("provider response shape changed")
            return parsed
    except (httpx.HTTPError, ValueError, UnicodeError, RecursionError):
        raise BootstrapError(f"Coolify {method} outcome uncertain; inspect before retry") from None


def one_named(items: list, name: str, *, permit_missing_uuid: bool = False) -> str | None:
    if len(items) > 1000:
        raise BootstrapError("provider inventory exceeds bound")
    matches = [item for item in items if isinstance(item, dict) and item.get("name") == name]
    if len(matches) > 1:
        raise BootstrapError("duplicate named resource requires operator reconciliation")
    if not matches:
        return None
    uuid = matches[0].get("uuid")
    if uuid is None and permit_missing_uuid:
        return ""
    if not isinstance(uuid, str) or not UUID.fullmatch(uuid):
        raise BootstrapError("named resource has no stable UUID")
    return uuid


async def bootstrap(client: httpx.AsyncClient, *, allow_create: bool) -> dict:
    created_any = False
    team = await response(client, "GET", "/api/v1/teams/current")
    if type(team.get("id")) is not int or team["id"] != 0:
        raise BootstrapError("Coolify owner team mismatch")
    projects = await response(client, "GET", "/api/v1/projects", result_type=list)
    project = one_named(projects, PROJECT_NAME)
    if project is None:
        if not allow_create:
            return {"status": "missing_project", "created": False}
        created = await response(client, "POST", "/api/v1/projects", expected=201,
                                 body={"name": PROJECT_NAME,
                                       "description": "Isolated generated-app pilot control plane"})
        created_any = True
        project = created.get("uuid")
        if not isinstance(project, str) or not UUID.fullmatch(project):
            raise BootstrapError("project create uncertain; reconcile before retry")
    route = "/api/v1/projects/" + project + "/environments"
    environments = await response(client, "GET", route, result_type=list)
    environment = one_named(environments, ENVIRONMENT_NAME, permit_missing_uuid=True)
    if environment == "":
        detail = await response(client, "GET", "/api/v1/projects/" + project + "/"
                                + ENVIRONMENT_NAME)
        environment = detail.get("uuid")
        if not isinstance(environment, str) or not UUID.fullmatch(environment):
            raise BootstrapError("named environment has no stable UUID")
    if environment is None:
        if not allow_create:
            return {"status": "missing_environment", "project_uuid": project,
                    "created": False}
        created = await response(client, "POST", route, expected=201,
                                 body={"name": ENVIRONMENT_NAME})
        created_any = True
        environment = created.get("uuid")
        if not isinstance(environment, str) or not UUID.fullmatch(environment):
            raise BootstrapError("environment create uncertain; reconcile before retry")
    verified = await response(client, "GET", "/api/v1/projects/" + project + "/"
                              + environment)
    if verified.get("name") != ENVIRONMENT_NAME or verified.get("uuid") != environment:
        raise BootstrapError("environment readback does not match created identity")
    return {"status": "verified", "project_uuid": project,
            "environment_uuid": environment, "created": created_any}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", default=".gca-local/coolify-configuration-deployment-v1.env")
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    token = private_token(Path(args.token_file))
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:18000",
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
        timeout=httpx.Timeout(20, connect=5), trust_env=False, follow_redirects=False,
    ) as client:
        try:
            result = await bootstrap(client, allow_create=args.create)
        except BootstrapError as error:
            raise SystemExit(str(error)) from None
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
