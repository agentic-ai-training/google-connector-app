import asyncio
from typing import Any

from googleapiclient.errors import HttpError

from app.db import google_clients as google


WRITE_TOOLS = {
    "send_gmail", "reply_gmail", "label_gmail", "trash_gmail",
    "create_calendar_event", "update_calendar_event", "delete_calendar_event",
    "upload_drive_file", "share_drive_file", "move_drive_file", "trash_drive_file",
    "create_google_doc", "append_to_google_doc", "write_google_sheet",
    "append_to_google_sheet", "create_google_sheet", "create_task", "complete_task",
    "send_chat_message", "create_meet_space",
}


def _first(data: dict, *keys: str):
    return next((data[key] for key in keys if data.get(key)), None)


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
            external_id = args.get("document_id") or args.get("spreadsheet_id") or external_id
        url = _first(
            result, "spreadsheetUrl", "documentUrl", "webViewLink", "htmlLink",
            "meetLink", "meetingUri", "url", "link",
        )
        if external_id or url:
            artifacts.append({
                "external_id": str(external_id) if external_id else None,
                "url": str(url) if url else None,
                "tool": tool,
            })
    return artifacts


def _read_after_write(tool: str, args: dict, result: dict) -> dict[str, Any]:
    if tool in {"send_gmail", "reply_gmail", "label_gmail", "trash_gmail"}:
        resource_id = _first(result, "id", "messageId") or args.get("message_id")
        value = google.gmail_service.users().messages().get(
            userId="me", id=resource_id, format="minimal"
        ).execute()
        return {"id": value.get("id"), "threadId": value.get("threadId")}
    if tool in {"create_calendar_event", "update_calendar_event"}:
        resource_id = _first(result, "id", "eventId") or args.get("event_id")
        value = google.calendar_service.events().get(
            calendarId="primary", eventId=resource_id
        ).execute()
        return {"id": value.get("id"), "status": value.get("status")}
    if tool == "delete_calendar_event":
        try:
            google.calendar_service.events().get(
                calendarId=args.get("calendar_id", "primary"),
                eventId=args["event_id"],
            ).execute()
        except HttpError as exc:
            if exc.resp.status in {404, 410}:
                return {"deleted": True, "id": args["event_id"]}
            raise
        raise RuntimeError("Calendar event still exists after deletion")
    if tool in {"upload_drive_file", "share_drive_file", "move_drive_file", "trash_drive_file"}:
        resource_id = _first(result, "fileId") or args.get("file_id") or result.get("id")
        value = google.drive_service.files().get(
            fileId=resource_id, fields="id,webViewLink,parents,permissionIds,trashed"
        ).execute()
        if tool == "trash_drive_file" and not value.get("trashed"):
            raise RuntimeError("Drive file still exists outside trash")
        return {"id": value.get("id"), "webViewLink": value.get("webViewLink")}
    if tool in {"create_google_doc", "append_to_google_doc"}:
        resource_id = _first(result, "documentId") or args.get("document_id")
        value = google.docs_service.documents().get(documentId=resource_id).execute()
        return {"documentId": value.get("documentId"), "title": value.get("title")}
    if tool in {"create_google_sheet", "write_google_sheet", "append_to_google_sheet"}:
        resource_id = _first(result, "spreadsheetId") or args.get("spreadsheet_id")
        value = google.sheets_service.spreadsheets().get(
            spreadsheetId=resource_id, fields="spreadsheetId,properties.title"
        ).execute()
        return {"spreadsheetId": value.get("spreadsheetId")}
    if tool in {"create_task", "complete_task"}:
        resource_id = _first(result, "id", "taskId") or args.get("task_id")
        value = google.tasks_service.tasks().get(
            tasklist=args.get("tasklist_id", "@default"), task=resource_id
        ).execute()
        return {"id": value.get("id"), "status": value.get("status")}
    if tool == "send_chat_message":
        resource_name = _first(result, "name", "id")
        value = google.chat_service.spaces().messages().get(name=resource_name).execute()
        return {"name": value.get("name"), "space": value.get("space")}
    if tool == "create_meet_space":
        resource_name = _first(result, "name")
        value = google.meet_service.spaces().get(name=resource_name).execute()
        return {"name": value.get("name"), "meetingUri": value.get("meetingUri")}
    return {"not_applicable": True}


async def verify_executions(executions: list[dict]) -> tuple[bool, str, list[dict]]:
    failures = [item for item in executions if isinstance(item.get("result"), dict)
                and (item["result"].get("error") or item["result"].get("success") is False)]
    if failures:
        return False, "At least one tool returned explicit failure evidence", []
    writes = [item for item in executions if item.get("tool") in WRITE_TOOLS]
    artifacts = extract_artifacts(executions)
    if not writes:
        return True, "Read-only tool postconditions passed", artifacts
    for execution in writes:
        result = execution.get("result")
        if not isinstance(result, dict):
            if execution["tool"] == "delete_calendar_event":
                result = {}
            else:
                return False, f"{execution['tool']} returned no structured evidence", artifacts
        try:
            evidence = await asyncio.to_thread(
                _read_after_write, execution["tool"], execution.get("arguments") or {}, result
            )
        except Exception as exc:
            return False, f"Read-after-write verification failed for {execution['tool']}: {exc}", artifacts
        if not evidence:
            return False, f"No verification evidence for {execution['tool']}", artifacts
    return True, "Tool-specific read-after-write postconditions passed", artifacts
