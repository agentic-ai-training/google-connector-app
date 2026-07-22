#!/usr/bin/env python3
"""Generate one governed draft in GitHub Actions without production credentials."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from groq import APIStatusError, APITimeoutError, RateLimitError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.improvements.builder import generate_candidate_draft, groq_bad_request_code
from app.improvements.network_guard import allowlisted_dns


def runtime_failure_code(exc: Exception) -> str | None:
    if not isinstance(exc, RuntimeError):
        return None
    message = str(exc)
    markers = {
        "history exceeded its bounded request budget": "history_budget_exhausted",
        "token budget exhausted during tool reasoning": "tool_token_budget_exhausted",
        "token budget exhausted before review": "review_token_budget_exhausted",
        "exceeded its bounded reasoning/tool rounds": "tool_round_limit_exhausted",
        "output was not valid JSON": "invalid_candidate_json",
        "Candidate contract failed local validation": "candidate_contract_invalid",
        "Reviewer contract failed local validation": "reviewer_contract_invalid",
        "Independent review rejected candidate": "independent_review_rejected",
    }
    for marker, code in markers.items():
        if marker.casefold() in message.casefold():
            return code
    return "bounded_runtime_failure"


def failure_payload(exc: Exception, stage: str) -> dict:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    retryable = isinstance(
        exc, (RateLimitError, APITimeoutError, httpx.TransportError, TimeoutError)
    ) or (
        isinstance(exc, APIStatusError) and (status == 429 or (status or 0) >= 500)
    ) or (
        isinstance(exc, httpx.HTTPStatusError) and (status == 429 or (status or 0) >= 500)
    )
    retry_after = None
    if response is not None:
        raw = response.headers.get("retry-after")
        try:
            retry_after = max(1, min(int(float(raw)), 86_400)) if raw else None
        except ValueError:
            retry_after = None
    if isinstance(exc, RateLimitError) and retry_after is None:
        body = getattr(exc, "body", None)
        error = body.get("error", body) if isinstance(body, dict) else {}
        message = str(error.get("message") or "") if isinstance(error, dict) else ""
        match = re.search(r"Please try again in ((?:[0-9.]+(?:ms|s|m|h))+)", message, re.I)
        if match:
            seconds = 0.0
            for amount, unit in re.findall(r"([0-9.]+)(ms|s|m|h)", match.group(1), re.I):
                seconds += float(amount) * {
                    "ms": 0.001, "s": 1, "m": 60, "h": 3600,
                }[unit.casefold()]
            retry_after = max(1, min(int(seconds + 0.999), 86_400))
    runtime_code = runtime_failure_code(exc)
    error_type = runtime_code or type(exc).__name__
    if isinstance(exc, RateLimitError):
        message = "Groq model quota is exhausted; retry after the provider reset."
    elif isinstance(exc, APIStatusError):
        if status == 400:
            error_type = groq_bad_request_code(exc)
        message = f"Groq API returned HTTP {status} during candidate {stage}."
    elif isinstance(exc, httpx.HTTPStatusError):
        message = f"Candidate callback returned HTTP {status} during {stage}."
    elif runtime_code:
        message = f"Candidate builder stopped at guard {runtime_code}."
    else:
        message = f"{type(exc).__name__} during candidate {stage}."
    return {
        "stage": stage, "error_type": error_type, "message": message,
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

                async def checkpoint_author(payload: dict) -> None:
                    checkpointed = await client.post(
                        f"{base}/admin/candidate-builder/{build_id}/checkpoint",
                        headers=headers, json=payload,
                    )
                    checkpointed.raise_for_status()

                candidate, tokens, roles, models_used = await asyncio.wait_for(
                    generate_candidate_draft(
                        job, checkpoint_callback=checkpoint_author,
                    ),
                    timeout=540,
                )
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
