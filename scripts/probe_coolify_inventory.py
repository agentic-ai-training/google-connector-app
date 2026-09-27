"""Read-only Coolify target proof. Never emits credentials or raw provider payloads."""
import argparse
import asyncio
import json
import stat
from pathlib import Path

from dotenv import dotenv_values

from app.coding.coolify import CoolifyAPIError, CoolifyInventory, CoolifyTarget


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--target-id", default="gca-delhi-coolify")
    parser.add_argument("--team-id", type=int, default=0)
    parser.add_argument("--token-file", default=".gca-local/coolify-inventory.env")
    args = parser.parse_args()
    path = Path(args.token_file)
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit("Token file must be a regular owner-only file, not a symlink")
    token = dotenv_values(path).get("COOLIFY_INVENTORY_TOKEN")
    # inventory_secret_ref is a lookup label; the actual credential was loaded above.
    target = CoolifyTarget(target_id=args.target_id, base_url=args.base_url,
                           team_id=args.team_id,
                           inventory_secret_ref="local/coolify-inventory")  # nosec B106
    try:
        async with CoolifyInventory(target, token or "") as client:
            servers = await client.servers()
        print(json.dumps({"target_id": target.target_id, "team_verified": True,
                          "servers": servers, "read_only": True,
                          "deployment_authorized": False}))
    except CoolifyAPIError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    asyncio.run(main())
