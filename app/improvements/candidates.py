"""Validation helpers for concrete governed-improvement candidates."""

import hashlib
import json
import re


ALLOWED_ROOTS = ("app/", "tests/", "knowledge/", "config/", "docs/", "web/")
FORBIDDEN_PARTS = (".env", "credentials", "oauth", "secret", "private_key")
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|refresh[_-]?token|client[_-]?secret)\s*[:=]"
)


def validate_candidate_files(files: list[dict]) -> list[str]:
    errors = []
    seen = set()
    if not files:
        return ["At least one concrete source, test, OKF, config, or documentation file is required"]
    for item in files:
        path = item["path"]
        if path in seen:
            errors.append(f"Duplicate candidate path: {path}")
        seen.add(path)
        if path.startswith("/") or ".." in path.split("/"):
            errors.append(f"Unsafe candidate path: {path}")
        if not path.startswith(ALLOWED_ROOTS):
            errors.append(f"Candidate path is outside approved roots: {path}")
        if any(part in path.casefold() for part in FORBIDDEN_PARTS):
            errors.append(f"Candidate path may contain credentials: {path}")
        content = item.get("content")
        if content and SECRET_PATTERN.search(content):
            errors.append(f"Candidate content contains a secret-like assignment: {path}")
    return errors


def candidate_digest(base_version: str, files: list[dict], validation_report: dict) -> str:
    canonical = json.dumps(
        {"base_version": base_version, "files": files,
         "validation_report": validation_report},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def file_digest(content: str | None) -> str:
    return hashlib.sha256((content or "").encode()).hexdigest()
