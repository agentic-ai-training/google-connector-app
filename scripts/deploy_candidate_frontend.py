"""Deploy an immutable Vercel preview only for a frozen frontend candidate."""

import json
import os
import re
import subprocess
from urllib.parse import urlparse

import httpx


surfaces = set(filter(None, os.environ.get("CANDIDATE_RUNTIME_SURFACES", "").split(",")))
output = {
    "frontend_deployment_id": None,
    "frontend_url": None,
    "frontend_source_commit": None,
}
if "frontend" in surfaces:
    version = os.environ["CANDIDATE_VERSION"]
    token = os.environ["VERCEL_TOKEN"]
    control_url = os.environ["CONTROL_FRONTEND_URL"].rstrip("/")
    command = [
        "npx", "-y", "vercel@latest", "deploy", "--yes", f"--token={token}",
        "--build-env", f"DEPLOYMENT_VERSION={version}",
        "--build-env", "CANDIDATE_FRONTEND_ROLE=candidate",
        "--env", f"DEPLOYMENT_VERSION={version}",
        "--env", "CANDIDATE_FRONTEND_ROLE=candidate",
        "--build-env", "NEXT_PUBLIC_CANDIDATE_FRONTEND=true",
        "--build-env", f"NEXT_PUBLIC_CONTROL_FRONTEND_URL={control_url}",
        "--meta", f"candidateVersion={version}",
        "--meta", f"proposalKey={os.environ['PROPOSAL_KEY']}",
    ]
    deployed = subprocess.check_output(command, text=True, stderr=subprocess.STDOUT)
    urls = re.findall(r"https://[a-zA-Z0-9-]+\.vercel\.app", deployed)
    if not urls:
        raise SystemExit("Vercel did not return an immutable preview URL")
    frontend_url = urls[-1].rstrip("/")
    hostname = urlparse(frontend_url).hostname
    response = httpx.get(
        f"https://api.vercel.com/v13/deployments/{hostname}",
        headers={"Authorization": f"Bearer {token}"},
        params={"teamId": os.environ["VERCEL_ORG_ID"]},
        timeout=30,
    )
    response.raise_for_status()
    inspected = response.json()
    deployment_id = inspected.get("id") or inspected.get("uid")
    if not deployment_id:
        raise SystemExit("Vercel preview has no immutable deployment identifier")
    if inspected.get("projectId") != os.environ["VERCEL_PROJECT_ID"]:
        raise SystemExit("Vercel preview belongs to the wrong project")
    if inspected.get("target") == "production":
        raise SystemExit("Candidate frontend must be a non-production preview")
    if (inspected.get("meta") or {}).get("candidateVersion") != version:
        raise SystemExit("Vercel deployment metadata does not match the candidate version")
    output = {
        "frontend_deployment_id": str(deployment_id),
        "frontend_url": frontend_url,
        "frontend_source_commit": version,
    }

with open("candidate-frontend.json", "w", encoding="utf-8") as handle:
    json.dump(output, handle)
print(json.dumps(output, sort_keys=True))
