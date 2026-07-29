"""Submit hash-bound candidate validation evidence from trusted GitHub Actions."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import httpx


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


manifests = sorted(Path(".improvement-proposals").glob("*.json"))
if len(manifests) != 1:
    raise SystemExit("A candidate PR must contain exactly one machine manifest")
manifest = json.loads(manifests[0].read_text())
file_hashes = {path: sha256_file(Path(path)) for path in manifest["files"]}
tree_sha = subprocess.check_output(
    ["git", "rev-parse", f"{os.environ['COMMIT_SHA']}^{{tree}}"], text=True,
).strip()
log_digest = sha256_file(Path("candidate-tests.log"))
payload = {
    "commit_sha": os.environ["COMMIT_SHA"], "tree_sha": tree_sha,
    "repository": os.environ["REPOSITORY"], "workflow": os.environ["WORKFLOW_NAME"],
    "run_id": os.environ["RUN_ID"], "suite_version": "candidate-ci-v2",
    "commands": ["alembic upgrade head", "pytest tests/ -q",
                 "python scripts/run_golden_evals.py",
                 "python scripts/run_workflow_replays.py",
                 "python scripts/run_policy_evals.py",
                 "python scripts/run_context_packing_evals.py",
                 "python -m compileall -q app", "python -m flake8 app scripts tests migrations",
                 "bandit -q -r app scripts", "pip-audit -r requirements.txt",
                 "alembic downgrade 012 && alembic upgrade head",
                 "docker build -f Dockerfile.worker .", "docker build -f Dockerfile.builder .",
                 "npm run lint && npm run build", "flutter analyze && flutter test",
                 "flutter build apk --debug"],
    "results": {"passed": True}, "file_hashes": file_hashes,
    "log_digest": log_digest, "passed": True,
}
base = os.environ["CANDIDATE_ATTESTATION_URL"].rstrip("/")
response = httpx.post(
    f"{base}/admin/candidate-builds/{manifest['build_id']}/attestation",
    json=payload,
    headers={"X-Candidate-Attestation-Token": os.environ["CANDIDATE_CI_ATTESTATION_TOKEN"]},
    timeout=30,
)
response.raise_for_status()
print(json.dumps(response.json(), sort_keys=True))
