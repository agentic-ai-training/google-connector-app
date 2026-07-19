#!/usr/bin/env python3
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.connection import close_pool, get_pool
from app.okf.loader import sync_bundle


async def main():
    pool = await get_pool()
    try:
        print(f"okf_documents_synced={await sync_bundle(pool)}")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
