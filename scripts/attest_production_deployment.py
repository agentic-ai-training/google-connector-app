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
        "npx", "-y", "@railway/cli@latest", "status", "--json",
    ], text=True))
    environments = value.get("environments", {}).get("edges", [])
    for environment in environments:
        services = environment.get("node", {}).get("serviceInstances", {}).get("edges", [])
        for entry in services:
            node = entry.get("node", {})
            if node.get("serviceName") != service:
                continue
            active = [
                item for item in node.get("activeDeployments", [])
                if item.get("status") == "SUCCESS"
                and item.get("instances")
                and all(instance.get("status") == "RUNNING" for instance in item["instances"])
                and (item.get("meta") or {}).get("imageDigest", "").startswith("sha256:")
            ]
            if active:
                return max(active, key=lambda item: item.get("createdAt", ""))
            return node.get("latestDeployment") or {}
    return {}


def worker_matches(deployment: dict) -> bool:
    logs = subprocess.check_output([
        "npx", "-y", "@railway/cli@latest", "logs", deployment["id"],
        "--service", SERVICES[1], "--lines", "200", "--json",
    ], text=True)
    return (
        "worker_ready role=control" in logs
        and f"deployment_version={COMMIT}" in logs
    )


def api_matches(_deployment: dict) -> bool:
    try:
        response = httpx.get(
            os.environ["CANDIDATE_ATTESTATION_URL"].rstrip("/") + "/health",
            timeout=15,
        )
        body = response.json()
        return (
            response.is_success and body.get("status") == "ok"
            and body.get("deployment_version") == COMMIT
            and body.get("executor_role") == "control"
        )
    except (httpx.HTTPError, ValueError):
        return False


def wait_for_service(service: str, version_matches) -> dict:
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
            and version_matches(deployment)
        ):
            return deployment
        time.sleep(10)
    raise SystemExit(f"Production {service} did not run commit {COMMIT} within ten minutes")


api = wait_for_service(SERVICES[0], api_matches)
worker = wait_for_service(SERVICES[1], worker_matches)

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
