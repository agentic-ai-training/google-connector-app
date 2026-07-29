"""Operation-specific, content-free read-after-write verification."""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from googleapiclient.errors import HttpError

from app.db import google_clients as google
from app.tools.contracts import WRITE_TOOLS


@dataclass
class VerificationOutcome:
    passed: bool
    message: str
    artifacts: list[dict] = field(default_factory=list)
    category: str | None = None
    component: str = "step_verifier"
    boundary: str = "postcondition_verification"
    evidence: dict = field(default_factory=dict)


def _first(data: dict, *keys: str):
    return next((data[key] for key in keys if data.get(key)), None)


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalize_rows(values: Any) -> list[list[str]]:
    rows = []
    for raw_row in values if isinstance(values, list) else []:
        row = ["" if value is None else str(value) for value in (
            raw_row if isinstance(raw_row, list) else [raw_row]
        )]
        while row and row[-1] == "":
            row.pop()
        rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _shape(rows: list[list]) -> tuple[int, int]:
    return len(rows), max((len(row) for row in rows), default=0)


def _safe_id(value: Any) -> str | None:
    return str(value)[:300] if value else None


def extract_artifacts(executions: list[dict]) -> list[dict]:
    artifacts = []
    for execution in executions:
        result = execution.get("result")
        if not isinstance(result, dict) or result.get("error"):
            continue
        args = execution.get("arguments") or {}
        tool = execution.get("tool", "unknown")
        external_id = _first(
            result, "spreadsheetId", "documentId", "fileId", "messageId",
            "eventId", "taskId", "spaceId", "conferenceId", "id", "name",
        )
        if tool == "share_drive_file":
            external_id = args.get("file_id") or external_id
        if tool in {"append_to_google_doc", "write_google_sheet", "append_to_google_sheet"}:
            external_id = (
                args.get("document_id") or args.get("spreadsheet_id") or external_id
            )
        url = _first(
            result, "spreadsheetUrl", "documentUrl", "webViewLink", "htmlLink",
            "meetLink", "meetingUri", "url", "link",
        )
        if external_id or url:
            identifiers = {
                key: value for key, value in args.items()
                if key in {
                    "file_id", "document_id", "spreadsheet_id", "event_id",
                    "calendar_id", "message_id", "task_id", "tasklist_id",
                    "space_name", "space_id",
                }
            }
            metadata = {"tool": tool, "identifiers": identifiers}
            if tool == "share_drive_file" and result.get("id"):
                metadata["permission_id"] = str(result["id"])
            artifacts.append({
                "external_id": str(external_id) if external_id else None,
                "url": str(url) if url else None,
                "tool": tool,
                "metadata": metadata,
                "safe_to_delete": tool in {
                    "upload_drive_file", "create_google_doc", "create_google_sheet",
                },
            })
    return artifacts


def _sheet_verification(tool: str, args: dict, result: dict) -> tuple[bool, dict]:
    spreadsheet_id = _first(result, "spreadsheetId") or args.get("spreadsheet_id")
    metadata = google.sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="spreadsheetId,properties.title",
    ).execute()
    evidence = {
        "tool": tool,
        "spreadsheet_id": _safe_id(spreadsheet_id),
        "readback_id": _safe_id(metadata.get("spreadsheetId")),
    }
    if metadata.get("spreadsheetId") != spreadsheet_id:
        return False, {**evidence, "match": False}
    if tool == "create_google_sheet":
        return True, {**evidence, "match": True}
    requested_range = args.get("range")
    if tool == "append_to_google_sheet":
        requested_range = (result.get("updates") or {}).get("updatedRange")
    if not requested_range:
        return False, {**evidence, "reason": "exact_range_unavailable", "match": False}
    expected = _normalize_rows(args.get("values"))
    observed_raw = google.sheets_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=requested_range,
    ).execute().get("values", [])
    observed = _normalize_rows(observed_raw)
    expected_shape = _shape(expected)
    observed_shape = _shape(observed)
    matches = expected == observed
    return matches, {
        **evidence,
        "range": str(requested_range)[:300],
        "expected_rows": expected_shape[0],
        "expected_columns": expected_shape[1],
        "observed_rows": observed_shape[0],
        "observed_columns": observed_shape[1],
        "expected_content_hash": _hash(expected),
        "observed_content_hash": _hash(observed),
        "match": matches,
    }


def _calendar_verification(tool: str, args: dict, result: dict) -> tuple[bool, dict]:
    event_id = _first(result, "id", "eventId") or args.get("event_id")
    calendar_id = args.get("calendar_id", "primary")
    if tool == "delete_calendar_event":
        try:
            google.calendar_service.events().get(
                calendarId=calendar_id, eventId=event_id,
            ).execute()
        except HttpError as exc:
            if exc.resp.status in {404, 410}:
                return True, {
                    "tool": tool, "event_id": _safe_id(event_id), "deleted": True,
                }
            raise
        return False, {
            "tool": tool, "event_id": _safe_id(event_id), "deleted": False,
        }
    value = google.calendar_service.events().get(
        calendarId=calendar_id, eventId=event_id,
    ).execute()
    expected_attendees = sorted(
        str(email).casefold() for email in (args.get("attendees") or [])
    )
    observed_attendees = sorted(
        str(item.get("email", "")).casefold()
        for item in value.get("attendees", []) if item.get("email")
    )
    expected_start = args.get("start_datetime")
    expected_end = args.get("end_datetime")
    timezone = args.get("timezone")
    observed_start = (value.get("start") or {}).get("dateTime")
    observed_end = (value.get("end") or {}).get("dateTime")
    observed_timezone = (
        (value.get("start") or {}).get("timeZone")
        or (value.get("end") or {}).get("timeZone")
    )
    conference_requested = bool(args.get("add_meet"))
    conference_present = bool(
        value.get("hangoutLink")
        or value.get("conferenceData", {}).get("entryPoints")
    )
    checks = {
        "id_match": value.get("id") == event_id,
        "active": value.get("status") != "cancelled",
        "start_match": expected_start is None or observed_start == expected_start,
        "end_match": expected_end is None or observed_end == expected_end,
        "timezone_match": timezone is None or observed_timezone == timezone,
        "attendees_match": not expected_attendees
        or expected_attendees == observed_attendees,
        "conference_match": not conference_requested or conference_present,
    }
    return all(checks.values()), {
        "tool": tool,
        "calendar_id": _safe_id(calendar_id),
        "event_id": _safe_id(event_id),
        "summary_hash": _hash(args.get("title")) if args.get("title") else None,
        "expected_attendee_hash": _hash(expected_attendees),
        "observed_attendee_hash": _hash(observed_attendees),
        **checks,
    }


def _chat_verification(args: dict, result: dict) -> tuple[bool, dict]:
    resource_name = _first(result, "name", "id")
    value = google.chat_service.spaces().messages().get(name=resource_name).execute()
    expected_space = (
        result.get("resolvedSpace")
        or args.get("space_id")
        or args.get("space_name")
    )
    observed_space = value.get("space")
    if isinstance(observed_space, dict):
        observed_space = observed_space.get("name")
    expected_text = str(args.get("text") or "")
    observed_text = str(value.get("text") or "")
    url_pattern = r"https?://[^\s<>()]+"
    expected_urls = sorted(re.findall(url_pattern, expected_text))
    observed_urls = sorted(re.findall(url_pattern, observed_text))
    checks = {
        "resource_match": value.get("name") == resource_name,
        "space_match": not expected_space or observed_space == expected_space,
        "text_match": _hash(expected_text) == _hash(observed_text),
        "references_match": expected_urls == observed_urls,
    }
    return all(checks.values()), {
        "tool": "send_chat_message",
        "message_name": _safe_id(resource_name),
        "space_name": _safe_id(observed_space),
        "expected_text_hash": _hash(expected_text),
        "observed_text_hash": _hash(observed_text),
        "expected_reference_count": len(expected_urls),
        "observed_reference_count": len(observed_urls),
        **checks,
    }


def _chat_space_verification(args: dict, result: dict) -> tuple[bool, dict]:
    resource_name = _first(result, "name")
    value = google.chat_service.spaces().get(name=resource_name).execute()
    observed_name = value.get("name")
    matches = bool(
        resource_name
        and re.fullmatch(r"spaces/[^/]+", str(resource_name))
        and observed_name == resource_name
    )
    return matches, {
        "tool": "resolve_chat_destination",
        "space_name": _safe_id(observed_name),
        "kind": str(result.get("kind") or "")[:100],
        "created": bool(result.get("created")),
        "resolver_readback_verified": result.get("readbackVerified") is True,
        "match": matches,
    }


def _drive_share_verification(args: dict, result: dict) -> tuple[bool, dict]:
    file_id = args.get("file_id")
    google.drive_service.files().get(fileId=file_id, fields="id").execute()
    permission_id = result.get("id")
    if permission_id:
        permission = google.drive_service.permissions().get(
            fileId=file_id, permissionId=permission_id,
            fields="id,type,role,emailAddress,domain,deleted",
        ).execute()
    else:
        permissions = google.drive_service.permissions().list(
            fileId=file_id,
            fields="permissions(id,type,role,emailAddress,domain,deleted)",
        ).execute().get("permissions", [])
        principal = args.get("email") or args.get("domain")
        permission = next((
            item for item in permissions
            if (item.get("emailAddress") or item.get("domain")) == principal
            and item.get("role") == args.get("role", "reader")
            and not item.get("deleted")
        ), {})
    expected_type = args.get("permission_type", "user")
    expected_principal = args.get("email") or args.get("domain")
    observed_principal = permission.get("emailAddress") or permission.get("domain")
    checks = {
        "type_match": permission.get("type") == expected_type,
        "role_match": permission.get("role") == args.get("role", "reader"),
        "principal_match": observed_principal == expected_principal,
        "active": not permission.get("deleted", False),
    }
    return all(checks.values()), {
        "tool": "share_drive_file",
        "file_id": _safe_id(file_id),
        "permission_id": _safe_id(permission.get("id")),
        "expected_principal_hash": _hash(expected_principal),
        "observed_principal_hash": _hash(observed_principal),
        **checks,
    }


def _generic_verification(tool: str, args: dict, result: dict) -> tuple[bool, dict]:
    if tool in {"send_gmail", "reply_gmail", "label_gmail", "trash_gmail"}:
        resource_id = _first(result, "id", "messageId") or args.get("message_id")
        value = google.gmail_service.users().messages().get(
            userId="me", id=resource_id, format="minimal",
        ).execute()
        return value.get("id") == resource_id, {
            "tool": tool, "message_id": _safe_id(resource_id),
            "id_match": value.get("id") == resource_id,
        }
    if tool in {"upload_drive_file", "move_drive_file", "trash_drive_file"}:
        resource_id = _first(result, "fileId") or args.get("file_id") or result.get("id")
        value = google.drive_service.files().get(
            fileId=resource_id, fields="id,webViewLink,parents,trashed",
        ).execute()
        matches = value.get("id") == resource_id
        if tool == "trash_drive_file":
            matches = matches and bool(value.get("trashed"))
        return matches, {
            "tool": tool, "file_id": _safe_id(resource_id), "match": matches,
        }
    if tool in {"create_google_doc", "append_to_google_doc"}:
        resource_id = _first(result, "documentId") or args.get("document_id")
        value = google.docs_service.documents().get(documentId=resource_id).execute()
        return value.get("documentId") == resource_id, {
            "tool": tool, "document_id": _safe_id(resource_id),
            "id_match": value.get("documentId") == resource_id,
        }
    if tool in {"create_task", "complete_task"}:
        resource_id = _first(result, "id", "taskId") or args.get("task_id")
        value = google.tasks_service.tasks().get(
            tasklist=args.get("tasklist_id", "@default"), task=resource_id,
        ).execute()
        expected_status = "completed" if tool == "complete_task" else None
        matches = value.get("id") == resource_id and (
            expected_status is None or value.get("status") == expected_status
        )
        return matches, {
            "tool": tool, "task_id": _safe_id(resource_id), "match": matches,
        }
    if tool == "create_meet_space":
        resource_name = _first(result, "name")
        value = google.meet_service.spaces().get(name=resource_name).execute()
        matches = (
            value.get("name") == resource_name and bool(value.get("meetingUri"))
        )
        return matches, {
            "tool": tool, "space_name": _safe_id(resource_name),
            "meeting_uri_present": bool(value.get("meetingUri")), "match": matches,
        }
    return True, {"tool": tool, "not_applicable": True}


def _verify_write(tool: str, args: dict, result: dict) -> tuple[bool, dict]:
    if tool in {"create_google_sheet", "write_google_sheet", "append_to_google_sheet"}:
        return _sheet_verification(tool, args, result)
    if tool in {
        "create_calendar_event", "update_calendar_event", "delete_calendar_event",
    }:
        return _calendar_verification(tool, args, result)
    if tool == "send_chat_message":
        return _chat_verification(args, result)
    if tool == "resolve_chat_destination":
        return _chat_space_verification(args, result)
    if tool == "share_drive_file":
        return _drive_share_verification(args, result)
    return _generic_verification(tool, args, result)


async def verify_executions_detailed(
    executions: list[dict], *, service: str | None = None,
    operation: str | None = None,
) -> VerificationOutcome:
    artifacts = extract_artifacts(executions)
    failures = [
        item for item in executions
        if not isinstance(item.get("result"), dict)
        or item["result"].get("error")
        or item["result"].get("success") is False
    ]
    if failures:
        return VerificationOutcome(
            False, "At least one required tool returned explicit failure evidence",
            artifacts, "tool_failure", "service_agent", "write_tool_execution",
            {
                "service": service, "operation": operation,
                "failed_tools": [str(item.get("tool")) for item in failures][:20],
            },
        )
    writes = [item for item in executions if item.get("tool") in WRITE_TOOLS]
    if not writes:
        return VerificationOutcome(
            True, "Read-only tool postconditions passed", artifacts,
            evidence={"service": service, "operation": operation},
        )
    evidence = []
    for execution in writes:
        tool = str(execution.get("tool"))
        result = execution.get("result")
        if not isinstance(result, dict):
            return VerificationOutcome(
                False, f"{tool} returned no structured evidence", artifacts,
                "tool_failure", "step_verifier", "write_tool_result",
                {"service": service, "operation": operation, "tool": tool},
            )
        try:
            passed, item_evidence = await asyncio.to_thread(
                _verify_write, tool, execution.get("arguments") or {}, result,
            )
        except Exception as exc:
            return VerificationOutcome(
                False, f"Read-after-write verification failed for {tool}", artifacts,
                "postcondition_failure", "step_verifier", "read_after_write",
                {
                    "service": service, "operation": operation, "tool": tool,
                    "error_type": type(exc).__name__,
                },
            )
        evidence.append(item_evidence)
        if not passed:
            return VerificationOutcome(
                False, f"Expected state did not match readback for {tool}", artifacts,
                "postcondition_failure", "step_verifier", "expected_state_mismatch",
                {
                    "service": service, "operation": operation, "tool": tool,
                    "checks": item_evidence,
                },
            )
    return VerificationOutcome(
        True, "Tool-specific read-after-write postconditions passed", artifacts,
        evidence={
            "service": service, "operation": operation,
            "verified_tools": [item["tool"] for item in evidence],
            "checks": evidence,
        },
    )


async def verify_executions(
    executions: list[dict],
) -> tuple[bool, str, list[dict]]:
    outcome = await verify_executions_detailed(executions)
    return outcome.passed, outcome.message, outcome.artifacts
