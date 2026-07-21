import json
import os
import subprocess
import time

import httpx


service = os.environ["RAILWAY_CANDIDATE_WORKER_SERVICE"]
candidate_version = os.environ["CANDIDATE_VERSION"]
surfaces = set(filter(None, os.environ.get("CANDIDATE_RUNTIME_SURFACES", "").split(",")))
with open("candidate-domain.json", encoding="utf-8") as handle:
    deployment_url = json.load(handle).get("deployment_url")
healthy = None
for _ in range(60):
    value = json.loads(subprocess.check_output([
        "npx", "-y", "@railway/cli@latest", "status", "--json",
    ], text=True))
    deployment = None
    for environment in value.get("environments", {}).get("edges", []):
        for entry in environment.get("node", {}).get("serviceInstances", {}).get("edges", []):
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
            deployment = (
                max(active, key=lambda item: item.get("createdAt", ""))
                if active else node.get("latestDeployment")
            )
            break
    deployment = deployment or {}
    status = deployment.get("status")
    if status == "SUCCESS":
        meta = deployment.get("meta") or {}
        instances = deployment.get("instances") or []
        if not meta.get("imageDigest", "").startswith("sha256:"):
            raise SystemExit("Candidate deployment has no immutable image digest")
        if not instances or any(item.get("status") != "RUNNING" for item in instances):
            time.sleep(10)
            continue
        logs = subprocess.check_output([
            "npx", "-y", "@railway/cli@latest", "logs", deployment["id"],
            "--service", service, "--lines", "200", "--json",
        ], text=True)
        ready = (
            ("candidate_runtime_ready role=candidate" in logs
             or "worker_ready role=candidate" in logs)
            and f"executor_version={candidate_version}" in logs
            and "Traceback" not in logs
        )
        if "api" in surfaces:
            if not deployment_url:
                raise SystemExit("API candidate has no isolated HTTPS deployment URL")
            try:
                response = httpx.get(deployment_url.rstrip("/") + "/health", timeout=15)
                health = response.json()
                ready = ready and response.is_success and (
                    health.get("status") == "ok"
                    and health.get("executor_role") == "candidate"
                    and health.get("executor_version") == candidate_version
                )
            except (httpx.HTTPError, ValueError):
                ready = False
        if not ready:
            time.sleep(10)
            continue
        healthy = {"latestDeployment": deployment}
        break
    if status in {"FAILED", "CRASHED", "REMOVED"}:
        raise SystemExit(f"Candidate deployment failed: {status}")
    time.sleep(10)
else:
    raise SystemExit("Candidate deployment did not become healthy within ten minutes")

with open("candidate-deployment.json", "w", encoding="utf-8") as handle:
    json.dump(healthy, handle)
print(json.dumps(healthy))
