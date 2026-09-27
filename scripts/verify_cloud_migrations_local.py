"""Prove the full Alembic upgrade/rollback chain in a disposable loopback database."""
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg


async def main():
    dsn = os.getenv("COST_LEDGER_TEST_DSN", "")
    parsed = urlsplit(dsn)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.query or parsed.fragment:
        raise SystemExit("Set COST_LEDGER_TEST_DSN to a plain loopback PostgreSQL URL")
    name = "gca_migration_probe_" + uuid4().hex
    test_dsn = urlunsplit(parsed._replace(path="/" + name))
    root = Path(__file__).resolve().parents[1]
    connection = await asyncpg.connect(dsn)
    created = False
    try:
        await connection.execute(f'CREATE DATABASE "{name}"')
        created = True
        # Migration checks require no provider, OAuth or deployment credentials.
        env = {"PATH": os.environ.get("PATH", ""), "DATABASE_URL": test_dsn,
               "PYTHONPATH": str(root), "LANGCHAIN_TRACING_V2": "false"}
        for args in (("upgrade", "head"), ("downgrade", "002"), ("upgrade", "head")):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "alembic", *args, cwd=root, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise RuntimeError("migration proof exceeded its time limit") from None
            if process.returncode:
                # Avoid printing the DSN or provider data; only this empty DB is in scope.
                print(stderr.decode(errors="replace")[-4000:].replace(test_dsn, "[local-test-dsn]"))
                raise RuntimeError("migration proof failed")
            print("Passed: alembic " + " ".join(args), flush=True)
        probe = await asyncpg.connect(test_dsn)
        try:
            version = await probe.fetchval("SELECT version_num FROM alembic_version")
            print(f"Verified migration head: {version}", flush=True)
        finally:
            await probe.close()
    finally:
        if created:
            # This unique database was created by this invocation; never drop the source DB.
            await connection.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
