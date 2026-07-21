#!/usr/bin/env python3
"""Generate one governed draft in GitHub Actions without production credentials."""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from groq import APITimeoutError, RateLimitError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.improvements.builder import generate_candidate_draft
from app.improvements.network_guard import allowlisted_dns


def failure_payload(exc: Exception, stage: str) -> dict:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    retryable = isinstance(exc, (RateLimitError, APITimeoutError, httpx.TransportError)) or (
        isinstance(exc, httpx.HTTPStatusError) and (status == 429 or (status or 0) >= 500)
    )
    retry_after = None
    if response is not None:
        raw = response.headers.get("retry-after")
        try:
            retry_after = max(1, min(int(float(raw)), 86_400)) if raw else None
        except ValueError:
            retry_after = None
    if isinstance(exc, RateLimitError):
        message = "Groq model quota is exhausted; retry after the provider reset."
    elif isinstance(exc, httpx.HTTPStatusError):
        message = f"Candidate callback returned HTTP {status} during {stage}."
    else:
        message = f"{type(exc).__name__} during candidate {stage}."
    return {
        "stage": stage, "error_type": type(exc).__name__, "message": message,
        "retryable": retryable, "retry_after_seconds": retry_after,
    }


async def report_failure(base: str, build_id: str, headers: dict, payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"{base}/admin/candidate-builder/{build_id}/failure",
                headers=headers, json=payload,
            )
    except httpx.HTTPError:
        # GitHub still preserves the sanitized terminal exception if the callback is down.
        return


async def main() -> None:
    base = os.environ["CANDIDATE_BUILDER_URL"].rstrip("/")
    build_id = os.environ["CANDIDATE_BUILD_ID"]
    headers = {
        "X-Candidate-Builder-Token": os.environ["CANDIDATE_BUILDER_CALLBACK_TOKEN"],
    }
    callback_host = urlparse(base).hostname
    with allowlisted_dns({"api.groq.com", callback_host or ""}):
        stage = "input"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{base}/admin/candidate-builder/{build_id}/input", headers=headers,
                )
                response.raise_for_status()
                job = response.json()["build"]
                stage = "generation"
                candidate, tokens, roles, models_used = await generate_candidate_draft(job)
                payload = {
                    "files": candidate.get("files") or [],
                    "exact_diff": candidate["exact_diff"],
                    "rollback_plan": candidate["rollback_plan"],
                    "validation_commands": candidate.get("validation_commands") or [],
                    "roles_completed": roles,
                    "models_used": models_used,
                    "tokens_used": tokens,
                }
                stage = "submission"
                submitted = await client.post(
                    f"{base}/admin/candidate-builder/{build_id}/draft",
                    headers=headers, json=payload,
                )
                submitted.raise_for_status()
                print(json.dumps(submitted.json(), sort_keys=True))
        except Exception as exc:
            failure = failure_payload(exc, stage)
            await report_failure(base, build_id, headers, failure)
            raise RuntimeError(
                f"Candidate builder stopped at {stage}: {failure['error_type']}"
            ) from None


if __name__ == "__main__":
    asyncio.run(main())
