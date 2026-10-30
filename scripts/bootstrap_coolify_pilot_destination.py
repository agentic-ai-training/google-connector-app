"""Operator-only one-shot pilot Docker destination enrollment over private IAP.

This creates no user Service or public ingress. A lost POST response is uncertain:
inspect the provider and never automatically repeat the mutation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from scripts.bootstrap_coolify_pilot import BootstrapError, UUID, private_token, response


SERVER_UUID = "frdwr0tdglbolht3kqtokzqa"
NETWORK = "gca-app-login-pilot-20260930"


def one_destination(items: list) -> str | None:
    if len(items) > 1000:
        raise BootstrapError("destination inventory exceeds bound")
    matches = [item for item in items if isinstance(item, dict)
               and item.get("network") == NETWORK]
    if len(matches) > 1:
        raise BootstrapError("duplicate pilot destination requires reconciliation")
    if not matches:
        return None
    item = matches[0]
    uuid = item.get("uuid")
    if (not isinstance(uuid, str) or not UUID.fullmatch(uuid)
            or item.get("server_uuid") != SERVER_UUID
            or item.get("type") != "standalone"):
        raise BootstrapError("pilot destination identity differs from approved host")
    return uuid


async def bootstrap(client: httpx.AsyncClient, *, allow_create: bool) -> dict:
    team = await response(client, "GET", "/api/v1/teams/current")
    if type(team.get("id")) is not int or team["id"] != 0:
        raise BootstrapError("Coolify owner team mismatch")
    server = await response(client, "GET", "/api/v1/servers/" + SERVER_UUID)
    if server.get("uuid") != SERVER_UUID:
        raise BootstrapError("Coolify server identity mismatch")
    destinations = await response(client, "GET", "/api/v1/destinations", result_type=list)
    uuid = one_destination(destinations)
    created = False
    if uuid is None:
        if not allow_create:
            return {"status": "missing_destination", "created": False}
        item = await response(client, "POST", "/api/v1/servers/" + SERVER_UUID
                              + "/destinations", expected=201,
                              body={"name": "GCA Login Pilot", "network": NETWORK,
                                    "type": "standalone"})
        uuid = item.get("uuid")
        created = True
        if not isinstance(uuid, str) or not UUID.fullmatch(uuid):
            raise BootstrapError("destination create uncertain; inspect before retry")
    detail = await response(client, "GET", "/api/v1/destinations/" + uuid)
    if (detail.get("uuid") != uuid or detail.get("server_uuid") != SERVER_UUID
            or detail.get("network") != NETWORK or detail.get("type") != "standalone"):
        raise BootstrapError("destination readback does not match approved identity")
    return {"status": "verified", "destination_uuid": uuid, "network": NETWORK,
            "server_uuid": SERVER_UUID, "created": created}


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
