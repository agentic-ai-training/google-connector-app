#!/usr/bin/env python3
"""Generate one governed draft in GitHub Actions without production credentials."""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.improvements.builder import generate_candidate_draft
from app.improvements.network_guard import allowlisted_dns


async def main() -> None:
    base = os.environ["CANDIDATE_BUILDER_URL"].rstrip("/")
    build_id = os.environ["CANDIDATE_BUILD_ID"]
    headers = {
        "X-Candidate-Builder-Token": os.environ["CANDIDATE_BUILDER_CALLBACK_TOKEN"],
    }
    callback_host = urlparse(base).hostname
    with allowlisted_dns({"api.groq.com", callback_host or ""}):
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{base}/admin/candidate-builder/{build_id}/input", headers=headers,
            )
            response.raise_for_status()
            job = response.json()["build"]
            candidate, tokens, roles = await generate_candidate_draft(job)
            payload = {
                "files": candidate.get("files") or [],
                "exact_diff": candidate["exact_diff"],
                "rollback_plan": candidate["rollback_plan"],
                "validation_commands": candidate.get("validation_commands") or [],
                "roles_completed": roles,
                "tokens_used": tokens,
            }
            submitted = await client.post(
                f"{base}/admin/candidate-builder/{build_id}/draft",
                headers=headers, json=payload,
            )
            if submitted.is_error:
                # API validation contains repository paths/schema errors only;
                # generated contents and private incident evidence are not logged.
                print(
                    json.dumps({
                        "draft_submission_status": submitted.status_code,
                        "validation_error": submitted.text[:4_000],
                    }),
                    file=sys.stderr,
                )
            submitted.raise_for_status()
            print(json.dumps(submitted.json(), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
