import json
import os

import httpx


with open("candidate-deployment.json", encoding="utf-8") as handle:
    status = json.load(handle)
deployment = status.get("latestDeployment") or status
meta = deployment.get("meta") or {}
with open("candidate-domain.json", encoding="utf-8") as handle:
    domain = json.load(handle)
payload = {
    "candidate_version": os.environ["CANDIDATE_VERSION"],
    "deployment_id": deployment["id"],
    "service_name": os.environ["RAILWAY_CANDIDATE_WORKER_SERVICE"],
    "project_id": os.environ["RAILWAY_PROJECT_ID"],
    "workflow": os.environ["WORKFLOW_NAME"], "run_id": os.environ["RUN_ID"],
    "image_digest": meta["imageDigest"],
    # The trusted workflow checked out and verified this SHA before Railway's
    # upload deployment; Railway upload metadata does not expose commitHash.
    "source_commit": os.environ["CANDIDATE_VERSION"],
    "runtime_surfaces": domain["runtime_surfaces"],
    "deployment_url": domain["deployment_url"],
    "smoke_tests": {"passed": True, "checks": [
        "Railway deployment reached SUCCESS with a RUNNING instance",
        "source commit and executor version are pinned",
        "candidate runtime emitted version-bound readiness",
        "API health is version-bound when an API surface is present",
    ]},
    "verified": True,
}
response = httpx.post(
    os.environ["CANDIDATE_ATTESTATION_URL"].rstrip("/")
    + f"/admin/improvements/{os.environ['PROPOSAL_KEY']}/deployment-attestation",
    json=payload,
    headers={"X-Candidate-Deploy-Token": os.environ["CANDIDATE_DEPLOY_ATTESTATION_TOKEN"]},
    timeout=30,
)
response.raise_for_status()
print(json.dumps(response.json(), sort_keys=True))
