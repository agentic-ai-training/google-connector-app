"""Verify that the frozen Vercel candidate preview serves its exact version."""

import json
import os
import time

import httpx


surfaces = set(filter(None, os.environ.get("CANDIDATE_RUNTIME_SURFACES", "").split(",")))
with open("candidate-frontend.json", encoding="utf-8") as handle:
    frontend = json.load(handle)
if "frontend" not in surfaces:
    raise SystemExit(0)

url = str(frontend.get("frontend_url") or "").rstrip("/")
version = os.environ["CANDIDATE_VERSION"]
if not url.startswith("https://"):
    raise SystemExit("Frontend candidate has no HTTPS Vercel preview URL")
for _ in range(60):
    try:
        response = httpx.get(url + "/api/frontend-health", timeout=15)
        health = response.json()
        if (
            response.is_success
            and health.get("status") == "ok"
            and health.get("executor_role") == "candidate"
            and health.get("deployment_version") == version
        ):
            print(json.dumps({"frontend_verified": True, **frontend}, sort_keys=True))
            break
    except (httpx.HTTPError, ValueError):
        pass
    time.sleep(10)
else:
    raise SystemExit("Candidate frontend did not serve its exact version within ten minutes")
