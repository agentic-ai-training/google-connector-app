"""Deterministic, privacy-bounded projections for LLM-facing tool results."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from app.rag.context_packer import sanitize_untrusted_content
from app.utils.tokenizer import token_count, truncate_tokens


_IDENTIFIER_KEYS = {
    "id", "name", "messageId", "threadId", "fileId", "documentId",
    "spreadsheetId", "eventId", "taskId", "spaceId", "conferenceId",
    "spreadsheetUrl", "documentUrl", "webViewLink", "htmlLink", "meetLink",
    "meetingUri", "url", "link", "status", "trashed",
    "resolvedSpace",
}
_GMAIL_KEYS = {
    "id", "thread_id", "sender", "sender_name", "subject", "snippet",
    "received_at", "labels", "has_attachments", "attachment_names",
}
_SENDER_KEYS = {
    "message_id", "thread_id", "sender_name", "sender_email", "received_at",
}


@dataclass(frozen=True)
class ToolResultEnvelope:
    tool_name: str
    compact_result: Any
    item_count: int
    original_bytes: int
    projected_bytes: int
    estimated_tokens: int
    truncated: bool
    continuation: str | None = None
    full_result_reference: str | None = None
    projection_version: str = "tool-result-v1"

    def metadata(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("compact_result", None)
        return value


def estimate_tokens(value: Any) -> int:
    rendered = value if isinstance(value, str) else json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    )
    return token_count(rendered)


def _safe_text(value: Any, max_characters: int) -> tuple[str, bool]:
    cleaned, _ = sanitize_untrusted_content(str(value or ""))
    if len(cleaned) <= max_characters:
        return cleaned, False
    return cleaned[:max_characters].rstrip() + "…", True


def _bounded(
    value: Any, *, depth: int = 0, max_depth: int = 5,
    max_items: int = 30, max_string: int = 1_200,
) -> tuple[Any, bool]:
    if depth >= max_depth:
        return "[nested value omitted]", True
    if isinstance(value, dict):
        output = {}
        truncated = False
        for index, key in enumerate(sorted(value, key=str)):
            if index >= max_items:
                truncated = True
                break
            projected, changed = _bounded(
                value[key], depth=depth + 1, max_depth=max_depth,
                max_items=max_items, max_string=max_string,
            )
            output[str(key)] = projected
            truncated = truncated or changed
        return output, truncated
    if isinstance(value, (list, tuple)):
        projected = []
        truncated = len(value) > max_items
        for item in value[:max_items]:
            bounded, changed = _bounded(
                item, depth=depth + 1, max_depth=max_depth,
                max_items=max_items, max_string=max_string,
            )
            projected.append(bounded)
            truncated = truncated or changed
        return projected, truncated
    if isinstance(value, str):
        return _safe_text(value, max_string)
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    return _safe_text(value, max_string)


def _project_records(
    records: Any, keys: set[str], *, max_items: int = 50,
    excerpt_key: str | None = None, excerpt_chars: int = 500,
) -> tuple[Any, bool]:
    values = records if isinstance(records, list) else [records]
    output = []
    truncated = len(values) > max_items
    for item in values[:max_items]:
        if not isinstance(item, dict):
            compact, changed = _bounded(item, max_string=excerpt_chars)
            output.append(compact)
            truncated = truncated or changed
            continue
        compact = {key: item[key] for key in keys if item.get(key) is not None}
        if excerpt_key and item.get(excerpt_key):
            compact[excerpt_key], changed = _safe_text(
                item[excerpt_key], excerpt_chars,
            )
            truncated = truncated or changed
        output.append(compact)
        truncated = truncated or len(compact) < len(item)
    return output if isinstance(records, list) else (output[0] if output else {}), truncated


def _project(tool_name: str, result: Any) -> tuple[Any, bool]:
    if tool_name == "list_recent_gmail_senders":
        if isinstance(result, dict) and isinstance(result.get("senders"), list):
            senders, truncated = _project_records(
                result["senders"], _SENDER_KEYS, max_items=100,
            )
            return {
                "senders": senders,
                "requested": result.get("requested"),
                "returned": len(senders),
                "unique": bool(result.get("unique", True)),
                "scanned": result.get("scanned"),
            }, truncated
        return _project_records(result, _SENDER_KEYS, max_items=100)
    if tool_name == "search_gmail":
        return _project_records(
            result, _GMAIL_KEYS, max_items=30,
            excerpt_key="snippet", excerpt_chars=500,
        )
    if tool_name == "get_gmail_message" and isinstance(result, dict):
        compact = {key: result[key] for key in _GMAIL_KEYS if result.get(key) is not None}
        body, changed = _safe_text(result.get("body_plain", ""), 2_000)
        if body:
            compact["body_excerpt"] = body
        return compact, changed or "body_html" in result or len(compact) < len(result)
    if tool_name == "get_drive_file" and isinstance(result, dict):
        metadata = result.get("metadata") or {}
        compact_metadata = {
            key: metadata[key] for key in (
                "id", "name", "mimeType", "webViewLink", "modifiedTime", "parents",
            ) if metadata.get(key) is not None
        }
        excerpt, changed = _safe_text(result.get("content", ""), 4_000)
        return {"metadata": compact_metadata, "content_excerpt": excerpt}, (
            changed or len(compact_metadata) < len(metadata)
        )
    if tool_name == "read_google_doc":
        return _safe_text(result, 4_000)
    if tool_name == "read_google_sheet" and isinstance(result, list):
        compact, changed = _bounded(result, max_items=50, max_string=500)
        return compact, changed
    if isinstance(result, dict) and any(key in result for key in _IDENTIFIER_KEYS):
        compact, changed = _bounded(result, max_items=40, max_string=1_500)
        return compact, changed
    return _bounded(result)


def _fit_token_limit(value: Any, max_tokens: int) -> tuple[Any, bool]:
    if estimate_tokens(value) <= max_tokens:
        return value, False
    if isinstance(value, list):
        selected = []
        for item in value:
            candidate = [*selected, item]
            if estimate_tokens(candidate) > max_tokens:
                break
            selected.append(item)
        if selected:
            return selected, True
    if isinstance(value, dict):
        selected = {}
        priority = [key for key in value if key in _IDENTIFIER_KEYS]
        priority.extend(key for key in value if key not in priority)
        for key in priority:
            candidate = {**selected, key: value[key]}
            if estimate_tokens(candidate) > max_tokens:
                continue
            selected[key] = value[key]
        if selected:
            return selected, True
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    return truncate_tokens(rendered, max_tokens).rstrip() + "…", True


def project_tool_result(
    tool_name: str, result: Any, *, max_tokens: int = 2_000,
) -> ToolResultEnvelope:
    """Return a deterministic compact representation safe for model/dependency use."""
    raw = json.dumps(result, default=str, separators=(",", ":"))
    compact, truncated = _project(tool_name, result)
    compact, token_truncated = _fit_token_limit(compact, max(64, max_tokens))
    rendered = json.dumps(compact, default=str, separators=(",", ":"))
    item_count = len(result) if isinstance(result, (list, tuple)) else (
        len(result.get("senders", []))
        if isinstance(result, dict) and isinstance(result.get("senders"), list)
        else (1 if result is not None else 0)
    )
    return ToolResultEnvelope(
        tool_name=tool_name,
        compact_result=compact,
        item_count=item_count,
        original_bytes=len(raw.encode()),
        projected_bytes=len(rendered.encode()),
        estimated_tokens=estimate_tokens(compact),
        truncated=truncated or token_truncated,
    )
