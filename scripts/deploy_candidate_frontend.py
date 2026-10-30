"""Deploy an immutable Vercel preview only for a frozen frontend candidate."""

import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote, urlparse

import httpx

# This script is executed with -I from the trusted controller checkout. Add only
# that fixed checkout, never the candidate cwd/PYTHONPATH, for shared lifecycle code.
CONTROLLER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROLLER_ROOT))
from app.coding.owned_process import OwnedProcessError, run_owned


surfaces = set(filter(None, os.environ.get("CANDIDATE_RUNTIME_SURFACES", "").split(",")))
output = {
    "frontend_deployment_id": None,
    "frontend_url": None,
    "frontend_source_commit": None,
}
if "frontend" in surfaces:
    version = os.environ["CANDIDATE_VERSION"]
    token = os.environ["VERCEL_TOKEN"]
    organization = os.environ["VERCEL_ORG_ID"]
    project = os.environ["VERCEL_PROJECT_ID"]
    if not all((token, organization, project, os.environ.get("CANDIDATE_SOURCE_ROOT"))):
        raise SystemExit("Candidate frontend requires explicit existing project and organization")
    project_response = httpx.get(
        f"https://api.vercel.com/v9/projects/{quote(project, safe='')}",
        headers={"Authorization": f"Bearer {token}"}, params={"teamId": organization}, timeout=30,
    )
    if not project_response.is_success or project_response.json().get("id") != project:
        raise SystemExit("Candidate frontend project readback failed; no deployment attempted")
    control_url = os.environ["CONTROL_FRONTEND_URL"].rstrip("/")
    command = [
        "vercel", "deploy", "--yes", f"--token={token}",
        "--cwd", os.environ["CANDIDATE_SOURCE_ROOT"],
        "--build-env", f"DEPLOYMENT_VERSION={version}",
        "--build-env", "CANDIDATE_FRONTEND_ROLE=candidate",
        "--env", f"DEPLOYMENT_VERSION={version}",
        "--env", "CANDIDATE_FRONTEND_ROLE=candidate",
        "--build-env", "NEXT_PUBLIC_CANDIDATE_FRONTEND=true",
        "--build-env", f"NEXT_PUBLIC_CONTROL_FRONTEND_URL={control_url}",
        "--meta", f"candidateVersion={version}",
        "--meta", f"proposalKey={os.environ['PROPOSAL_KEY']}",
    ]
    with tempfile.TemporaryDirectory(prefix='gca-vercel-controller-') as home:
        environment = {'PATH': os.environ['PATH'], 'HOME': home, 'CI': 'true',
            'VERCEL_ORG_ID': organization, 'VERCEL_PROJECT_ID': project,
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}
        try:
            code, stdout = asyncio.run(run_owned(command, cwd=CONTROLLER_ROOT, env=environment,
                timeout=600, output_limit=2_097_152))
        except (OwnedProcessError, OSError):
            raise SystemExit("Candidate frontend deployment outcome requires reconciliation") from None
    if code:
        # Never include argv (containing the token) or private provider output.
        raise SystemExit("Candidate frontend deployment failed; outcome requires reconciliation")
    deployed = stdout.decode('utf-8', errors='replace')
    urls = re.findall(r"https://[a-zA-Z0-9-]+\.vercel\.app", deployed)
    if not urls:
        raise SystemExit("Vercel did not return an immutable preview URL")
    frontend_url = urls[-1].rstrip("/")
    hostname = urlparse(frontend_url).hostname
    response = httpx.get(
        f"https://api.vercel.com/v13/deployments/{hostname}",
        headers={"Authorization": f"Bearer {token}"},
        params={"teamId": organization},
        timeout=30,
    )
    response.raise_for_status()
    inspected = response.json()
    deployment_id = inspected.get("id") or inspected.get("uid")
    if not deployment_id:
        raise SystemExit("Vercel preview has no immutable deployment identifier")
    if inspected.get("projectId") != project:
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
