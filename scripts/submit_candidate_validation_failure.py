"""Send bounded, sanitized CI diagnostics into the governed remediation queue."""

import hashlib
import json
import os
import re
from pathlib import Path

import httpx


SECRET = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*\S+",
)
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
DIAGNOSTIC = re.compile(
    r"(FAILED|ERROR|Error:|error TS\d+|SyntaxError|AssertionError|"
    r"ModuleNotFoundError|E\s{2,}|lint|analy[sz]e|build failed)",
    re.IGNORECASE,
)


def sanitized_diagnostics(paths: list[Path]) -> tuple[list[str], str]:
    raw = b""
    lines = []
    for path in paths:
        if not path.is_file():
            continue
        data = path.read_bytes()[:2_000_000]
        raw += data
        for line in data.decode("utf-8", errors="replace").splitlines():
            if not DIAGNOSTIC.search(line):
                continue
            value = EMAIL.sub("[redacted-email]", line)
            value = SECRET.sub(r"\1=[redacted]", value)
            value = re.sub(r"/home/runner/work/[^/\s]+/[^/\s]+/", "", value)
            value = " ".join(value.split())[:600]
            if value and value not in lines:
                lines.append(value)
            if len(lines) >= 30:
                break
    return lines, hashlib.sha256(raw).hexdigest()


manifests = sorted(Path(".improvement-proposals").glob("*.candidate.json"))
if len(manifests) != 1:
    raise SystemExit("A candidate PR must contain exactly one machine manifest")
manifest = json.loads(manifests[0].read_text())
log_paths = sorted(Path("candidate-failure-logs").rglob("*.log"))
diagnostics, log_digest = sanitized_diagnostics(log_paths)
failed_jobs = [
    name for name in ("backend", "web", "flutter")
    if os.environ.get(f"{name.upper()}_RESULT") == "failure"
]
if not failed_jobs:
    failed_jobs = ["unknown"]
codes = [f"{name}_validation_failed" for name in failed_jobs]
if not diagnostics:
    diagnostics = [
        "Trusted CI failed before a bounded diagnostic line was available; "
        "inspect the affected validation surface and add a regression test."
    ]
payload = {
    "commit_sha": os.environ["COMMIT_SHA"],
    "repository": os.environ["REPOSITORY"],
    "workflow": os.environ["WORKFLOW_NAME"],
    "run_id": os.environ["RUN_ID"],
    "failed_jobs": failed_jobs,
    "diagnostic_codes": codes,
    "diagnostics": diagnostics,
    "log_digest": log_digest,
}
base = os.environ["CANDIDATE_ATTESTATION_URL"].rstrip("/")
response = httpx.post(
    f"{base}/admin/candidate-builds/{manifest['build_id']}/validation-failure",
    json=payload,
    headers={
        "X-Candidate-Attestation-Token":
            os.environ["CANDIDATE_CI_ATTESTATION_TOKEN"],
    },
    timeout=30,
)
response.raise_for_status()
print(json.dumps(response.json(), sort_keys=True))
