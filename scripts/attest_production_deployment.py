#!/usr/bin/env python3
"""Wait for exact production API/worker images and attest a governed promotion."""

import json
import os
import subprocess
import time

import httpx


COMMIT = os.environ["PRODUCTION_COMMIT"]
SERVICES = ("google-connector-app", "google-connector-worker")


def service_status(service: str) -> dict:
    value = json.loads(subprocess.check_output([
        "npx", "-y", "@railway/cli@latest", "service", "status", "--json",
        "--service", service,
    ], text=True))
    return value.get("latestDeployment") or value


def wait_for_service(service: str) -> dict:
    for _ in range(60):
        deployment = service_status(service)
        status = deployment.get("status")
        if status in {"FAILED", "CRASHED", "REMOVED"}:
            raise SystemExit(f"Production {service} deployment failed: {status}")
        meta = deployment.get("meta") or {}
        instances = deployment.get("instances") or []
        if (
            status == "SUCCESS"
            and meta.get("imageDigest", "").startswith("sha256:")
            and instances
            and all(item.get("status") == "RUNNING" for item in instances)
        ):
            return deployment
        time.sleep(10)
    raise SystemExit(f"Production {service} did not run commit {COMMIT} within ten minutes")


api = wait_for_service(SERVICES[0])
worker = wait_for_service(SERVICES[1])
logs = subprocess.check_output([
    "npx", "-y", "@railway/cli@latest", "logs", worker["id"],
    "--service", SERVICES[1], "--lines", "200", "--json",
], text=True)
if "worker_ready role=control" not in logs or f"deployment_version={COMMIT}" not in logs:
    raise SystemExit("Production worker readiness is not bound to the promoted commit")

base_url = os.environ["CANDIDATE_ATTESTATION_URL"].rstrip("/")
health = httpx.get(f"{base_url}/health", timeout=30)
health.raise_for_status()
health_body = health.json()
if not (
    health_body.get("status") == "ok"
    and health_body.get("deployment_version") == COMMIT
    and health_body.get("executor_role") == "control"
):
    raise SystemExit("Production API health is not bound to the promoted commit")

payload = {
    "production_commit": COMMIT,
    "project_id": os.environ["RAILWAY_PROJECT_ID"],
    "api_service": SERVICES[0],
    "api_deployment_id": api["id"],
    "api_image_digest": api["meta"]["imageDigest"],
    "worker_service": SERVICES[1],
    "worker_deployment_id": worker["id"],
    "worker_image_digest": worker["meta"]["imageDigest"],
    "workflow": os.environ["WORKFLOW_NAME"],
    "run_id": os.environ["RUN_ID"],
    "smoke_tests": {"passed": True, "checks": [
        "API and worker run the exact promoted commit",
        "API and worker have immutable image digests and running instances",
        "API health passed and worker emitted version-bound readiness",
    ]},
    "verified": True,
}
response = httpx.post(
    f"{base_url}/admin/improvements/production-attestation",
    json=payload,
    headers={
        "X-Candidate-Deploy-Token": os.environ["CANDIDATE_DEPLOY_ATTESTATION_TOKEN"],
    },
    timeout=30,
)
response.raise_for_status()
print(json.dumps(response.json(), sort_keys=True))
