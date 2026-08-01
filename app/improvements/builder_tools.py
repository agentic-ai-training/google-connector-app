"""Least-privilege, in-memory repository tools for the untrusted Groq builder."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
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


PROJECTED_STAGED_BODY = re.compile(
    r"^\[staged in memory; \d+ chars; sha256:[0-9a-f]{64}; body omitted\]$",
)


class BoundedRepositoryTools:
    """Expose bounded reads and in-memory proposals; never execute or write code."""

    def __init__(
        self, root: Path, *, max_calls: int = 60, max_read_bytes: int = 120_000,
        max_files: int = 12, max_elapsed_seconds: int = 180,
    ):
        self.root = root.resolve()
        self.max_calls = max_calls
        self.max_read_bytes = max_read_bytes
        self.max_files = max_files
        self.max_elapsed_seconds = max_elapsed_seconds
        self.calls = 0
        self.read_bytes = 0
        self.read_paths: set[str] = set()
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
            _tool(
                "index_repository_symbols",
                "Index Python classes and functions without reading whole files",
                {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "query": {"type": "string"},
                },
                [],
            ),
            _tool(
                "read_repository_symbol",
                "Read one Python class or function by qualified symbol name",
                {
                    "path": {"type": "string"},
                    "symbol": {"type": "string"},
                },
                ["path", "symbol"],
            ),
            _tool(
                "find_symbol_references",
                "Find bounded lexical references to a Python symbol",
                {
                    "symbol": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
                ["symbol"],
            ),
            _tool(
                "inspect_test_neighborhood",
                "Find tests and implementation references near a symbol or term",
                {"query": {"type": "string"}},
                ["query"],
            ),
            _tool(
                "localize_runtime_boundary",
                "Rank existing runtime and test surfaces for incident terms before reading source",
                {
                    "terms": {"type": "array", "items": {"type": "string"}},
                    "service": {"type": "string"},
                    "operation": {"type": "string"},
                },
                ["terms"],
            ),
            _tool("stage_candidate_file", "Stage an in-memory candidate file", {
                "path": {"type": "string"},
                "change_type": {"type": "string", "enum": ["create", "replace", "delete"]},
                "content": {"type": "string"},
            }, ["path", "change_type"]),
            _tool(
                "apply_candidate_patch",
                "Replace a bounded line range without restaging the whole file",
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 0},
                    "replacement": {"type": "string"},
                },
                ["path", "start_line", "end_line", "replacement"],
            ),
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

    def execute(self, name: str, arguments: dict | None) -> Any:
        self._charge()
        handlers = {
            "list_repository_files": self.list_files,
            "search_repository": self.search,
            "read_repository_file": self.read,
            "index_repository_symbols": self.index_symbols,
            "read_repository_symbol": self.read_symbol,
            "find_symbol_references": self.find_references,
            "inspect_test_neighborhood": self.inspect_test_neighborhood,
            "localize_runtime_boundary": self.localize_runtime_boundary,
            "stage_candidate_file": self.stage,
            "apply_candidate_patch": self.apply_patch,
            "inspect_candidate_diff": self.diff,
            "validate_staged_candidate": self.validate_staged,
            "inspect_candidate_manifest": self.manifest,
            "discard_staged_candidate_file": self.discard,
            "read_staged_candidate_file": self.read_staged,
            "design_tool_extension": self.design_tool_extension,
        }
        if name not in handlers:
            raise ValueError(f"Unknown builder tool: {name}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("Candidate builder tool arguments must be an object")
        return handlers[name](**arguments)

    def restore_counters(
        self, *, calls: int, read_bytes: int, read_paths: list[str] | None = None,
    ) -> None:
        """Restore durable limits without allowing a retry to replenish authority."""
        calls = int(calls)
        read_bytes = int(read_bytes)
        if not 0 <= calls <= self.max_calls:
            raise BuilderToolLimitError("invalid candidate tool-call checkpoint")
        if not 0 <= read_bytes <= self.max_read_bytes:
            raise BuilderToolLimitError("invalid candidate read-byte checkpoint")
        self.calls = calls
        self.read_bytes = read_bytes
        self.read_paths = {
            path for path in (read_paths or [])
            if isinstance(path, str) and any(path.startswith(root) for root in ALLOWED_ROOTS)
        }

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
            "index_repository_symbols", "find_symbol_references",
            "inspect_test_neighborhood", "localize_runtime_boundary",
        }:
            field = (
                "symbols" if "symbols" in value else
                "references" if "references" in value else "matches"
            )
            items = list(value.get(field) or [])
            value[field] = items[:50]
            value["truncated"] = bool(value.get("truncated")) or len(items) > 50
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
        if self.calls >= self.max_calls:
            raise BuilderToolLimitError("candidate repository tool-call limit exceeded")
        self.calls += 1
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
        self.read_paths.add(path)
        if self.read_bytes > self.max_read_bytes:
            raise BuilderToolLimitError("candidate repository read-byte limit exceeded")
        return {"path": path, "start_line": start, "end_line": end, "content": content}

    def _python_files(self, paths: list[str] | None = None):
        for root in (paths or ["app/", "tests/"])[:20]:
            prefix = root.strip().replace("\\", "/")
            if not prefix.startswith(ALLOWED_ROOTS):
                continue
            candidate = self.root / prefix
            files = [candidate] if candidate.is_file() else (
                candidate.rglob("*.py") if candidate.is_dir() else []
            )
            for path in files:
                if path.is_file() and path.stat().st_size <= 300_000:
                    yield path

    def index_symbols(
        self, paths: list[str] | None = None, query: str = "",
    ) -> dict:
        needle = query.casefold().strip()
        symbols = []
        for path in self._python_files(paths):
            relative = path.relative_to(self.root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if needle and needle not in node.name.casefold():
                    continue
                symbols.append({
                    "path": relative,
                    "symbol": node.name,
                    "kind": (
                        "class" if isinstance(node, ast.ClassDef) else
                        "async_function" if isinstance(node, ast.AsyncFunctionDef)
                        else "function"
                    ),
                    "start_line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                })
                if len(symbols) >= 300:
                    return {"symbols": symbols, "truncated": True}
        return {"symbols": symbols, "truncated": False}

    def read_symbol(self, path: str, symbol: str) -> dict:
        target = self._safe_path(path, must_exist=True)
        if target.suffix != ".py":
            raise ValueError("Symbol reads are available only for Python files")
        source = target.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path)
        matches = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol
        ]
        if len(matches) != 1:
            raise ValueError("Symbol must identify exactly one class or function")
        node = matches[0]
        return self.read(path, node.lineno, getattr(node, "end_lineno", node.lineno))

    def find_references(
        self, symbol: str, paths: list[str] | None = None,
    ) -> dict:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", symbol):
            raise ValueError("Symbol must be a Python identifier")
        result = self.search(symbol, paths or ["app/", "tests/"])
        return {
            "references": result["matches"],
            "truncated": result["truncated"],
        }

    def inspect_test_neighborhood(self, query: str) -> dict:
        implementation = self.search(query, ["app/"])["matches"][:20]
        tests = self.search(query, ["tests/"])["matches"][:30]
        return {
            "matches": [
                *({"surface": "implementation", **item} for item in implementation),
                *({"surface": "test", **item} for item in tests),
            ],
            "truncated": False,
        }

    def localize_runtime_boundary(
        self, terms: list[str], service: str = "", operation: str = "",
    ) -> dict:
        """Return compact ranked locations; callers must still read exact source."""
        needles = []
        for value in [service, operation, *(terms or [])]:
            value = str(value or "").casefold().strip()
            if 2 <= len(value) <= 100 and value not in needles:
                needles.append(value)
        if not needles:
            raise ValueError("At least one bounded localization term is required")
        scored: dict[tuple[str, int], dict] = {}
        for needle in needles[:12]:
            for match in self.search(needle, ["app/", "tests/"])["matches"][:60]:
                key = (match["path"], int(match["line"]))
                item = scored.setdefault(key, {
                    **match, "score": 0, "matched_terms": [],
                })
                item["score"] += 3 if needle in {service, operation} else 1
                item["matched_terms"].append(needle)
        ranked = sorted(
            scored.values(),
            key=lambda item: (-item["score"], item["path"], item["line"]),
        )[:60]
        return {
            "matches": ranked,
            "terms": needles,
            "next_required_action": (
                "Read the highest-relevance existing implementation symbol/file and "
                "its regression-test neighborhood before staging application code."
            ),
            "truncated": len(scored) > len(ranked),
        }

    def stage(self, path: str, change_type: str, content: str = "") -> dict:
        target = self._safe_path(path)
        exists = target.is_file()
        if change_type == "create" and exists:
            raise ValueError("create requires a path absent from the base repository")
        if change_type in {"replace", "delete"} and not exists:
            raise ValueError(
                f"{change_type} requires a file present in the base repository"
            )
        if change_type != "delete" and PROJECTED_STAGED_BODY.fullmatch(content.strip()):
            raise ValueError(
                "Projected staged-file provenance cannot become candidate source",
            )
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

    def apply_patch(
        self, path: str, start_line: int, end_line: int, replacement: str,
    ) -> dict:
        """Apply one explicit line-range replacement to an in-memory candidate."""
        target = self._safe_path(path, must_exist=True)
        existing = self.staged.get(path)
        if existing and existing["change_type"] == "delete":
            raise ValueError("Cannot patch a staged deletion")
        source = (
            str(existing.get("content") or "")
            if existing else target.read_text(encoding="utf-8")
        )
        lines = source.splitlines()
        start = int(start_line)
        end = int(end_line)
        if start < 1 or end < start - 1 or end > len(lines):
            raise ValueError("Patch line range is outside the candidate file")
        replacement_lines = replacement.splitlines()
        updated = "\n".join([
            *lines[:start - 1],
            *replacement_lines,
            *lines[end:],
        ])
        if source.endswith("\n") or replacement.endswith("\n"):
            updated += "\n"
        result = self.stage(path, "replace", updated)
        return {
            **result,
            "patched_range": {"start_line": start, "end_line": end},
            "replacement_lines": len(replacement_lines),
            "sha256": hashlib.sha256(updated.encode()).hexdigest(),
        }

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
