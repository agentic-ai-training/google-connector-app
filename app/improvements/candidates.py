"""Validation helpers for concrete governed-improvement candidates."""

import ast
import hashlib
import json
import re
from urllib.parse import urlparse


ALLOWED_ROOTS = (
    "app/", "tests/", "knowledge/", "config/", "docs/", "web/", "mobile/",
)
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
        if content and path.endswith(".py"):
            try:
                tree = ast.parse(content, filename=path)
            except SyntaxError:
                continue
            functions = [
                node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            if path.startswith("app/") and functions and all(
                all(isinstance(stmt, (ast.Pass, ast.Expr)) for stmt in node.body)
                for node in functions
            ):
                errors.append(f"Candidate application file is placeholder-only: {path}")
            tests = [node for node in functions if node.name.startswith("test")]
            for test in tests:
                meaningful = any(
                    isinstance(node, ast.Assert)
                    or (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and (
                            node.func.attr.startswith("assert")
                            or node.func.attr == "raises"
                        )
                    )
                    for node in ast.walk(test)
                )
                if not meaningful:
                    errors.append(f"Candidate test has no assertion: {path}:{test.name}")
    return errors


def validate_candidate_adoption(files: list[dict]) -> list[str]:
    """Reject code that is tested in isolation but cannot affect a runtime path."""
    created_app_code = [
        item.get("path", "") for item in files
        if item.get("change_type") == "create"
        and item.get("path", "").startswith("app/")
        and item.get("path", "").endswith(".py")
    ]
    adopted_by_existing_runtime = any(
        item.get("change_type") in {"replace", "delete"}
        and item.get("path", "").startswith("app/")
        and item.get("path", "").endswith(".py")
        for item in files
    )
    if created_app_code and not adopted_by_existing_runtime:
        return [
            "New application code must be integrated by changing at least one "
            "existing application runtime file"
        ]
    return []


def candidate_digest(
    base_version: str, files: list[dict], validation_report: dict, *,
    candidate_kind: str | None = None, candidate_version: str | None = None,
    exact_diff: str | None = None, rollback_plan: dict | None = None,
    deployment_evidence: dict | None = None,
) -> str:
    """Bind every reviewed artifact into one canonical approval digest."""
    canonical = json.dumps(
        {"base_version": base_version, "files": files,
         "candidate_kind": candidate_kind, "candidate_version": candidate_version,
         "exact_diff": exact_diff, "validation_report": validation_report,
         "rollback_plan": rollback_plan or {},
         "deployment_evidence": deployment_evidence or {}},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def file_digest(content: str | None) -> str:
    return hashlib.sha256((content or "").encode()).hexdigest()


def infer_candidate_kind(files: list[dict]) -> str:
    """Pure knowledge overlays use the independently governed OKF lifecycle."""
    paths = [item.get("path", "") for item in files]
    return "okf" if paths and all(
        path.startswith("knowledge/") and path.endswith(".md") for path in paths
    ) else "code"


def worker_canary_incompatible_paths(files: list[dict]) -> list[str]:
    """Return runtime paths whose behavior cannot be exercised by a worker-only canary."""
    neutral_prefixes = ("tests/", "docs/", "knowledge/")
    worker_prefixes = (
        "app/agents/", "app/tools/", "app/rag/", "app/okf/", "app/db/",
        "app/config/", "app/runs/worker.py", "app/runs/verifier.py",
        "app/runs/incident.py", "app/runs/informational.py", "app/evaluation/",
    )
    incompatible = []
    for item in files:
        path = item.get("path", "")
        if path.startswith(neutral_prefixes) or path.startswith(worker_prefixes):
            continue
        incompatible.append(path)
    return sorted(incompatible)


def candidate_runtime_surfaces(files: list[dict]) -> list[str]:
    """Classify immutable files into the runtime surfaces a canary must exercise."""
    surfaces = set()
    for item in files:
        path = item.get("path", "")
        if path.startswith("web/") or path.startswith("mobile/"):
            surfaces.add("frontend")
        elif path.startswith("app/api/") or path in {
            "app/api/main.py", "app/runs/planner.py", "app/runs/repository.py",
        }:
            surfaces.add("api")
        elif path.startswith("app/"):
            surfaces.add("worker")
    return sorted(surfaces or {"registry"})


def unsupported_candidate_surfaces(files: list[dict]) -> list[str]:
    """Return runtime surfaces with no isolated governed deployment target."""
    supported = {"api", "frontend", "registry", "worker"}
    return [surface for surface in candidate_runtime_surfaces(files)
            if surface not in supported]


def valid_candidate_frontend_url(value: str | None) -> bool:
    """Accept only origin-only immutable Vercel preview targets."""
    parsed = urlparse(value or "")
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.hostname.endswith(".vercel.app")
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )
