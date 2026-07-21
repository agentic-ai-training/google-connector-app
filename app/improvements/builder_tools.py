"""Least-privilege, in-memory repository tools for the untrusted Groq builder."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import time
import tomllib
from pathlib import Path
from typing import Any

import yaml

from app.improvements.candidates import (
    ALLOWED_ROOTS, FORBIDDEN_PARTS, validate_candidate_files,
)


class BuilderToolLimitError(RuntimeError):
    pass


class BoundedRepositoryTools:
    """Expose bounded reads and in-memory proposals; never execute or write code."""

    def __init__(
        self, root: Path, *, max_calls: int = 30, max_read_bytes: int = 120_000,
        max_files: int = 12, max_elapsed_seconds: int = 180,
    ):
        self.root = root.resolve()
        self.max_calls = max_calls
        self.max_read_bytes = max_read_bytes
        self.max_files = max_files
        self.max_elapsed_seconds = max_elapsed_seconds
        self.calls = 0
        self.read_bytes = 0
        self.started = time.monotonic()
        self.staged: dict[str, dict[str, Any]] = {}

    @staticmethod
    def schemas() -> list[dict]:
        return [
            _tool("list_repository_files", "List approved repository files", {
                "directory": {"type": "string"},
            }, ["directory"]),
            _tool("search_repository", "Literal text search in approved source files", {
                "query": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
            }, ["query"]),
            _tool("read_repository_file", "Read a bounded line range", {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            }, ["path"]),
            _tool("stage_candidate_file", "Stage an in-memory candidate file", {
                "path": {"type": "string"},
                "change_type": {"type": "string", "enum": ["create", "replace", "delete"]},
                "content": {"type": "string"},
            }, ["path", "change_type"]),
            _tool("inspect_candidate_diff", "Inspect the bounded in-memory candidate diff", {}, []),
            _tool(
                "validate_staged_candidate",
                "Run deterministic structural and syntax validation on staged files",
                {}, [],
            ),
            _tool("inspect_candidate_manifest", "Inspect staged paths, sizes, and hashes", {}, []),
            _tool("discard_staged_candidate_file", "Discard one staged change in memory", {
                "path": {"type": "string"},
            }, ["path"]),
            _tool("read_staged_candidate_file", "Read a bounded staged candidate line range", {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            }, ["path"]),
            _tool("design_tool_extension", "Return mandatory surfaces for a new tool proposal", {
                "name": {"type": "string"},
                "service": {"type": "string"},
                "purpose": {"type": "string"},
            }, ["name", "service", "purpose"]),
        ]

    def execute(self, name: str, arguments: dict) -> Any:
        self._charge()
        handlers = {
            "list_repository_files": self.list_files,
            "search_repository": self.search,
            "read_repository_file": self.read,
            "stage_candidate_file": self.stage,
            "inspect_candidate_diff": self.diff,
            "validate_staged_candidate": self.validate_staged,
            "inspect_candidate_manifest": self.manifest,
            "discard_staged_candidate_file": self.discard,
            "read_staged_candidate_file": self.read_staged,
            "design_tool_extension": self.design_tool_extension,
        }
        if name not in handlers:
            raise ValueError(f"Unknown builder tool: {name}")
        return handlers[name](**arguments)

    @staticmethod
    def project_result(name: str, result: Any, *, max_chars: int = 4_000) -> dict:
        """Project repository results before they enter provider conversation history."""
        value = dict(result) if isinstance(result, dict) else {"result": result}
        if name == "list_repository_files":
            files = list(value.get("files") or [])
            value["files"] = files[:150]
            value["truncated"] = bool(value.get("truncated")) or len(files) > 150
        elif name == "search_repository":
            matches = list(value.get("matches") or [])
            value["matches"] = matches[:30]
            value["truncated"] = bool(value.get("truncated")) or len(matches) > 30
        elif name in {
            "read_repository_file", "read_staged_candidate_file", "inspect_candidate_diff",
        }:
            field = "content" if "content" in value else "diff"
            text = str(value.get(field) or "")
            value[field] = text[:max_chars]
            value["truncated"] = bool(value.get("truncated")) or len(text) > max_chars
        rendered = json.dumps(value, default=str, sort_keys=True)
        if len(rendered) <= max_chars:
            return value
        return {
            "projected": True,
            "tool": name,
            "summary": rendered[:max_chars],
            "truncated": True,
        }

    def _charge(self) -> None:
        self.calls += 1
        if self.calls > self.max_calls:
            raise BuilderToolLimitError("candidate repository tool-call limit exceeded")
        if time.monotonic() - self.started > self.max_elapsed_seconds:
            raise BuilderToolLimitError("candidate repository tool time limit exceeded")

    def _safe_path(self, value: str, *, must_exist: bool = False) -> Path:
        normalized = value.strip().replace("\\", "/").lstrip("./")
        if not normalized.startswith(ALLOWED_ROOTS) or ".." in normalized.split("/"):
            raise ValueError(f"Repository path is outside approved roots: {value}")
        if any(part in normalized.casefold() for part in FORBIDDEN_PARTS):
            raise ValueError(f"Repository path may contain credentials: {value}")
        path = (self.root / normalized).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Repository path escapes checkout: {value}") from exc
        if must_exist and not path.is_file():
            raise ValueError(f"Repository file does not exist: {value}")
        return path

    def list_files(self, directory: str) -> dict:
        prefix = directory.strip().replace("\\", "/").rstrip("/") + "/"
        if not prefix.startswith(ALLOWED_ROOTS):
            raise ValueError("Directory is outside approved roots")
        files = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
            and path.relative_to(self.root).as_posix().startswith(prefix)
            and not any(part.startswith(".") for part in path.relative_to(self.root).parts)
        ]
        return {"files": sorted(files)[:500], "truncated": len(files) > 500}

    def search(self, query: str, paths: list[str] | None = None) -> dict:
        needle = query.casefold().strip()
        if not needle or len(needle) > 200:
            raise ValueError("Search query must contain 1-200 characters")
        roots = paths or list(ALLOWED_ROOTS)
        matches = []
        for root in roots[:20]:
            prefix = root.strip().replace("\\", "/")
            if not prefix.startswith(ALLOWED_ROOTS):
                continue
            candidate = self.root / prefix
            files = [candidate] if candidate.is_file() else candidate.rglob("*") if candidate.is_dir() else []
            for path in files:
                if not path.is_file() or path.stat().st_size > 300_000:
                    continue
                try:
                    path.resolve().relative_to(self.root)
                except ValueError:
                    continue
                relative = path.relative_to(self.root).as_posix()
                if any(part in relative.casefold() for part in FORBIDDEN_PARTS):
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(lines, 1):
                    if needle in line.casefold():
                        matches.append({
                            "path": relative,
                            "line": number, "excerpt": line[:300],
                        })
                        if len(matches) >= 200:
                            return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def read(self, path: str, start_line: int = 1, end_line: int = 400) -> dict:
        target = self._safe_path(path, must_exist=True)
        start = max(1, int(start_line))
        end = min(max(start, int(end_line)), start + 799)
        lines = target.read_text(encoding="utf-8").splitlines()
        content = "\n".join(lines[start - 1:end])
        size = len(content.encode())
        self.read_bytes += size
        if self.read_bytes > self.max_read_bytes:
            raise BuilderToolLimitError("candidate repository read-byte limit exceeded")
        return {"path": path, "start_line": start, "end_line": end, "content": content}

    def stage(self, path: str, change_type: str, content: str = "") -> dict:
        self._safe_path(path)
        if len(content.encode()) > 500_000:
            raise BuilderToolLimitError("candidate file size limit exceeded")
        item = {
            "path": path, "change_type": change_type,
            "content": None if change_type == "delete" else content,
        }
        errors = validate_candidate_files([item])
        if errors:
            raise ValueError("; ".join(errors))
        if path not in self.staged and len(self.staged) >= self.max_files:
            raise BuilderToolLimitError("candidate changed-file limit exceeded")
        projected_total = sum(
            len((value.get("content") or "").encode())
            for key, value in self.staged.items() if key != path
        ) + len(content.encode())
        if projected_total > 1_500_000:
            raise BuilderToolLimitError("candidate aggregate output limit exceeded")
        self.staged[path] = item
        return {"staged": path, "change_type": change_type, "file_count": len(self.staged)}

    def diff(self) -> dict:
        output = []
        for item in self.staged.values():
            path = item["path"]
            target = self.root / path
            before = target.read_text(encoding="utf-8") if target.is_file() else ""
            after = item.get("content") or ""
            output.extend(difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
            ))
        rendered = "\n".join(output)
        return {"diff": rendered[:100_000], "truncated": len(rendered) > 100_000}

    def manifest(self) -> dict:
        files = []
        for item in sorted(self.staged.values(), key=lambda value: value["path"]):
            content = item.get("content") or ""
            files.append({
                "path": item["path"],
                "change_type": item["change_type"],
                "bytes": len(content.encode()),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            })
        return {"files": files, "file_count": len(files)}

    def discard(self, path: str) -> dict:
        self._safe_path(path)
        existed = path in self.staged
        self.staged.pop(path, None)
        return {"discarded": path, "existed": existed, "file_count": len(self.staged)}

    def validate_staged(self) -> dict:
        """Validate staged syntax without importing or executing candidate code."""
        policy_errors = validate_candidate_files(self.staged_files())
        errors = [{"code": "candidate_policy", "detail": value[:300]} for value in policy_errors]
        checked = []
        for item in sorted(self.staged.values(), key=lambda value: value["path"]):
            path = item["path"]
            if item["change_type"] == "delete":
                checked.append({"path": path, "validator": "delete_policy"})
                continue
            content = item.get("content") or ""
            suffix = Path(path).suffix.casefold()
            validator = "text"
            try:
                if suffix == ".py":
                    validator = "python_ast"
                    ast.parse(content, filename=path)
                elif suffix == ".json":
                    validator = "json"
                    json.loads(content)
                elif suffix in {".yaml", ".yml"}:
                    validator = "yaml_safe_load"
                    yaml.safe_load(content)
                elif suffix == ".toml":
                    validator = "tomllib"
                    tomllib.loads(content)
            except (SyntaxError, ValueError, TypeError, yaml.YAMLError) as exc:
                problem_mark = getattr(exc, "problem_mark", None)
                errors.append({
                    "path": path,
                    "code": f"{validator}_invalid",
                    "line": (
                        getattr(exc, "lineno", None)
                        or (getattr(problem_mark, "line", -1) + 1 if problem_mark else None)
                    ),
                })
            checked.append({"path": path, "validator": validator})
        return {
            "valid": not errors,
            "checked": checked,
            "errors": errors[:50],
            "manifest": self.manifest(),
            "authority": "structural_only_trusted_ci_still_required",
        }

    def read_staged(
        self, path: str, start_line: int = 1, end_line: int = 400,
    ) -> dict:
        self._safe_path(path)
        if path not in self.staged or self.staged[path]["change_type"] == "delete":
            raise ValueError(f"Staged candidate file is unavailable: {path}")
        start = max(1, int(start_line))
        end = min(max(start, int(end_line)), start + 799)
        lines = (self.staged[path].get("content") or "").splitlines()
        content = "\n".join(lines[start - 1:end])
        size = len(content.encode())
        self.read_bytes += size
        if self.read_bytes > self.max_read_bytes:
            raise BuilderToolLimitError("candidate repository read-byte limit exceeded")
        return {
            "path": path, "source": "staged_candidate",
            "start_line": start, "end_line": end, "content": content,
        }

    @staticmethod
    def design_tool_extension(name: str, service: str, purpose: str) -> dict:
        return {
            "untrusted_design_only": True,
            "name": name, "service": service, "purpose": purpose,
            "required_surfaces": [
                "typed tool schema and compact return schema",
                "Google adapter with least OAuth scopes and preconditions",
                "registry entry and planner operation mapping",
                "projection allowlist and token bound",
                "deterministic verifier and idempotency behavior",
                "no-network unit, planner, workflow replay, and permission tests",
                "draft OKF capability/workflow concepts",
            ],
            "authority": "Cannot register, execute, authorize, or publish a tool",
        }

    def staged_files(self) -> list[dict]:
        return list(self.staged.values())


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name, "description": description,
            "parameters": {
                "type": "object", "properties": properties,
                "required": required, "additionalProperties": False,
            },
        },
    }
