"""Read-only installed-Coolify proof of the approved pilot target identities.

No grant, cost reservation, Service create, deploy, or secret read occurs here.
The dummy secret references are schema placeholders, not credentials.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path

from app.coding.coolify_service import CoolifyServiceClient, CoolifyServiceError
from app.coding.deployment_admission import RegisteredTarget
from app.coding.operator_secrets import (
    OperatorSecretError, OperatorTokenResolver, TokenBinding,
)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", default=".gca-local/coolify-inventory-deployment-v1.env")
    args = parser.parse_args()
    resolver = OperatorTokenResolver((TokenBinding(
        reference="local/coolify-inventory", variable="COOLIFY_INVENTORY_TOKEN",
        source="private_file", path=Path(args.token_file),
        expires_at=datetime.fromisoformat("2026-10-19T17:24:32+00:00"),
    ),))
    target = RegisteredTarget(
        target_id="gca-delhi-login-pilot-probe", base_url="http://127.0.0.1:18000",
        team_id=0, project_id="project-ee0c1944-144c-4364-ad4",
        server_uuid="frdwr0tdglbolht3kqtokzqa", server_id=0,
        destination_uuid="2rxr0cpp0x91g47o9flwcbzk", destination_id=1,
        destination_type="App\\Models\\StandaloneDocker",
        destination_network="gca-app-login-pilot-20260930",
        coolify_project_uuid="mnv84tsssicw2y7dtogvbmwr",
        environment_uuid="pi3o2fpjo2ituxrppav8izj4",
        owner_user_id="operator-read-only-probe",
        # These are lookup labels in an unregistered probe target, not credentials.
        inventory_secret_ref="local/coolify-inventory",  # nosec B106
        configuration_secret_ref="probe/configuration",
        deployment_secret_ref="probe/deployment",
        recovery_secret_ref="probe/recovery",
    )
    phase = "team"
    try:
        token = resolver(target.inventory_secret_ref)
        async with CoolifyServiceClient(target, token or "") as client:
            await client.verify_team()
            phase = "project_environment"
            await client._verified_target_ids()
            phase = "destination"
            await client._verify_isolated_destination()
    except OperatorSecretError as error:
        raise SystemExit(f"credential:{error.category}") from None
    except CoolifyServiceError as error:
        if phase == "project_environment":
            async with CoolifyServiceClient(target, token or "") as client:
                server = await client._json(
                    "GET", "/api/v1/servers/" + target.server_uuid, expected=200,
                )
                project = await client._json(
                    "GET", "/api/v1/projects/" + target.coolify_project_uuid,
                    expected=200,
                )
                environment = await client._json(
                    "GET", "/api/v1/projects/" + target.coolify_project_uuid
                    + "/" + target.environment_uuid, expected=200,
                )
            print(json.dumps({
                "phase": phase,
                "server": {key: server.get(key) for key in ("id", "uuid")},
                "project": {key: project.get(key) for key in ("id", "uuid")},
                "environment": {key: environment.get(key)
                                for key in ("id", "uuid", "project_id")},
            }))
        raise SystemExit(f"{phase}:{error.category}") from None
    print(json.dumps({
        "team_verified": True, "target_verified": True,
        "server_uuid": target.server_uuid, "project_uuid": target.coolify_project_uuid,
        "environment_uuid": target.environment_uuid,
        "destination_uuid": target.destination_uuid,
        "destination_network": target.destination_network,
        "provider_write": False,
    }))


if __name__ == "__main__":
    asyncio.run(main())
