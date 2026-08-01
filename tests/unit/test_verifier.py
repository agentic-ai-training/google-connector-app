import asyncio
import base64
from types import SimpleNamespace

from app.improvements.failure_intelligence import sanitize_failure_evidence
from app.runs.verifier import verify_executions_detailed
from app.runs.reconciliation import reconcile_failed_step
from app.runs.verifier import VerificationOutcome


class Reply:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def _run(executions, service, operation):
    return asyncio.run(verify_executions_detailed(
        executions, service=service, operation=operation,
    ))


def _sheet_service(metadata, values):
    value_api = SimpleNamespace(get=lambda **_: Reply({"values": values}))
    spreadsheet_api = SimpleNamespace(
        get=lambda **_: Reply(metadata),
        values=lambda: value_api,
    )
    return SimpleNamespace(spreadsheets=lambda: spreadsheet_api)


def test_sheet_creation_verifies_stable_id(monkeypatch):
    monkeypatch.setattr(
        "app.runs.verifier.google.sheets_service",
        _sheet_service({"spreadsheetId": "sheet-1"}, []),
    )
    outcome = _run([{
        "tool": "create_google_sheet", "arguments": {"title": "private"},
        "result": {"spreadsheetId": "sheet-1"},
    }], "sheets", "create")
    assert outcome.passed
    assert "private" not in str(outcome.evidence)


def test_sheet_write_exact_readback_and_mismatch(monkeypatch):
    monkeypatch.setattr(
        "app.runs.verifier.google.sheets_service",
        _sheet_service({"spreadsheetId": "sheet-1"}, [["Name"], ["Ada"]]),
    )
    execution = {
        "tool": "write_google_sheet",
        "arguments": {
            "spreadsheet_id": "sheet-1", "range": "A1:A2",
            "values": [["Name"], ["Ada"]],
        },
        "result": {"spreadsheetId": "sheet-1"},
    }
    passed = _run([execution], "sheets", "write")
    assert passed.passed
    assert passed.evidence["checks"][0]["expected_content_hash"]
    execution["arguments"]["values"] = [["Name"], ["Grace"]]
    failed = _run([execution], "sheets", "write")
    assert not failed.passed
    assert failed.category == "postcondition_failure"


def test_sheet_append_uses_provider_updated_range(monkeypatch):
    monkeypatch.setattr(
        "app.runs.verifier.google.sheets_service",
        _sheet_service({"spreadsheetId": "sheet-1"}, [["Ada"]]),
    )
    outcome = _run([{
        "tool": "append_to_google_sheet",
        "arguments": {"spreadsheet_id": "sheet-1", "values": [["Ada"]]},
        "result": {
            "spreadsheetId": "sheet-1",
            "updates": {"updatedRange": "Sheet1!A9:A9"},
        },
    }], "sheets", "append")
    assert outcome.passed
    assert outcome.evidence["checks"][0]["range"] == "Sheet1!A9:A9"


def _calendar_service(event):
    events = SimpleNamespace(get=lambda **_: Reply(event))
    return SimpleNamespace(events=lambda: events)


def _gmail_service(message):
    messages = SimpleNamespace(get=lambda **_: Reply(message))
    users = SimpleNamespace(messages=lambda: messages)
    return SimpleNamespace(users=lambda: users)


def _gmail_message(message_id, recipient, subject, body):
    encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return {
        "id": message_id,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "To", "value": recipient},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": encoded},
        },
    }


def test_list_valued_gmail_search_is_valid_read_evidence():
    outcome = _run([{
        "tool": "search_gmail",
        "arguments": {
            "query": "to:source@example.com in:sent",
            "max_results": 1,
        },
        "result": [{"id": "source-1", "snippet": "bounded"}],
    }, {
        "tool": "get_gmail_message",
        "arguments": {"message_id": "source-1"},
        "result": {
            "id": "source-1",
            "subject": "Exact subject",
            "body_plain": "Exact body",
        },
    }], "gmail", "search")

    assert outcome.passed
    assert outcome.message == "Read-only tool postconditions passed"


def test_read_error_mapping_and_non_mapping_write_still_fail():
    read_failure = _run([{
        "tool": "search_gmail",
        "arguments": {"query": "in:sent"},
        "result": {"error": "provider unavailable"},
    }], "gmail", "search")
    write_failure = _run([{
        "tool": "send_gmail",
        "arguments": {"to": "destination@example.com"},
        "result": [{"id": "not-a-valid-write-contract"}],
    }], "gmail", "send")

    assert not read_failure.passed
    assert read_failure.category == "tool_failure"
    assert not write_failure.passed
    assert write_failure.category == "tool_failure"


def test_gmail_send_verifies_recipient_subject_and_body(monkeypatch):
    message = _gmail_message(
        "sent-1", "destination@example.com", "Exact subject", "Exact body",
    )
    monkeypatch.setattr(
        "app.runs.verifier.google.gmail_service", _gmail_service(message),
    )
    execution = {
        "tool": "send_gmail",
        "arguments": {
            "to": "destination@example.com",
            "subject": "Exact subject",
            "body": "Exact body",
        },
        "result": {"id": "sent-1"},
    }

    passed = _run([execution], "gmail", "send")
    assert passed.passed
    assert "Exact body" not in str(passed.evidence)
    assert passed.evidence["checks"][0]["body_match"]

    execution["arguments"]["body"] = "Different body"
    failed = _run([execution], "gmail", "send")
    assert not failed.passed
    assert failed.category == "postcondition_failure"


def test_calendar_expected_state_and_mismatch(monkeypatch):
    event = {
        "id": "event-1", "status": "confirmed",
        "start": {"dateTime": "2026-07-28T10:00:00+05:30",
                  "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": "2026-07-28T10:30:00+05:30",
                "timeZone": "Asia/Kolkata"},
        "attendees": [{"email": "guest@example.com"}],
        "hangoutLink": "https://meet.google.com/example",
    }
    monkeypatch.setattr(
        "app.runs.verifier.google.calendar_service", _calendar_service(event),
    )
    execution = {
        "tool": "create_calendar_event",
        "arguments": {
            "title": "private title",
            "start_datetime": event["start"]["dateTime"],
            "end_datetime": event["end"]["dateTime"],
            "timezone": "Asia/Kolkata",
            "attendees": ["guest@example.com"], "add_meet": True,
        },
        "result": {"id": "event-1"},
    }
    assert _run([execution], "calendar", "create").passed
    execution["arguments"]["timezone"] = "UTC"
    failed = _run([execution], "calendar", "create")
    assert not failed.passed
    assert failed.category == "postcondition_failure"
    assert "private title" not in str(failed.evidence)


def _chat_service(message):
    messages = SimpleNamespace(get=lambda **_: Reply(message))
    spaces = SimpleNamespace(
        messages=lambda: messages,
        get=lambda **kwargs: Reply({"name": kwargs["name"]}),
    )
    return SimpleNamespace(spaces=lambda: spaces)


def test_chat_destination_text_and_reference(monkeypatch):
    text = "Use https://docs.google.com/spreadsheets/d/sheet-1"
    monkeypatch.setattr(
        "app.runs.verifier.google.chat_service",
        _chat_service({"name": "spaces/s/messages/m", "space": "spaces/s", "text": text}),
    )
    execution = {
        "tool": "send_chat_message",
        "arguments": {"space_id": "spaces/s", "text": text},
        "result": {"name": "spaces/s/messages/m"},
    }
    assert _run([execution], "chat", "send").passed
    execution["arguments"]["space_id"] = "spaces/other"
    assert not _run([execution], "chat", "send").passed


def test_chat_email_destination_verifies_resolved_space(monkeypatch):
    text = "hi"
    monkeypatch.setattr(
        "app.runs.verifier.google.chat_service",
        _chat_service({
            "name": "spaces/direct/messages/m",
            "space": "spaces/direct",
            "text": text,
        }),
    )
    execution = {
        "tool": "send_chat_message",
        "arguments": {"space_id": "person@example.com", "text": text},
        "result": {
            "name": "spaces/direct/messages/m",
            "resolvedSpace": "spaces/direct",
        },
    }
    assert _run([execution], "chat", "send").passed


def test_chat_destination_resolution_is_read_back(monkeypatch):
    monkeypatch.setattr(
        "app.runs.verifier.google.chat_service",
        _chat_service({}),
    )
    execution = {
        "tool": "resolve_chat_destination",
        "arguments": {"destination": "person@example.com"},
        "result": {
            "name": "spaces/direct",
            "kind": "direct_message",
            "created": True,
            "readbackVerified": True,
        },
    }

    outcome = _run([execution], "chat", "send")

    assert outcome.passed
    assert outcome.evidence["checks"][0] == {
        "tool": "resolve_chat_destination",
        "space_name": "spaces/direct",
        "kind": "direct_message",
        "created": True,
        "resolver_readback_verified": True,
        "match": True,
    }


def _drive_service(permission):
    permissions = SimpleNamespace(
        get=lambda **_: Reply(permission),
        list=lambda **_: Reply({"permissions": [permission]}),
    )
    files = SimpleNamespace(get=lambda **_: Reply({"id": "file-1"}))
    return SimpleNamespace(
        files=lambda: files,
        permissions=lambda: permissions,
    )


def test_drive_permission_expected_state_and_mismatch(monkeypatch):
    permission = {
        "id": "permission-1", "type": "user", "role": "reader",
        "emailAddress": "guest@example.com", "deleted": False,
    }
    monkeypatch.setattr(
        "app.runs.verifier.google.drive_service", _drive_service(permission),
    )
    execution = {
        "tool": "share_drive_file",
        "arguments": {
            "file_id": "file-1", "email": "guest@example.com",
            "permission_type": "user", "role": "reader",
        },
        "result": permission,
    }
    assert _run([execution], "drive", "share").passed
    execution["arguments"]["role"] = "writer"
    failed = _run([execution], "drive", "share")
    assert not failed.passed
    assert failed.category == "postcondition_failure"


def test_explicit_tool_failure_is_typed_without_readback():
    outcome = _run([{
        "tool": "write_google_sheet", "arguments": {},
        "result": {"error": "provider rejected"},
    }], "sheets", "write")
    assert not outcome.passed
    assert outcome.category == "tool_failure"
    assert outcome.boundary == "write_tool_execution"


def test_explicit_read_failure_is_not_mislabeled_as_write_execution():
    outcome = _run([{
        "tool": "search_gmail", "arguments": {"query": "category:promotions"},
        "result": {"error": "Google API 400: Invalid maxResults Value"},
    }], "gmail", "message_count")
    assert not outcome.passed
    assert outcome.category == "tool_failure"
    assert outcome.boundary == "read_tool_execution"
    assert "invalid maxResults" in outcome.message
    assert outcome.evidence["provider_failure_classes"] == [
        "Google API 400: invalid maxResults value",
    ]


def test_failure_evidence_redacts_private_values_and_keeps_hashes():
    sanitized = sanitize_failure_evidence({
        "service": "chat",
        "text": "private chat text",
        "expected_text_hash": "abc123",
        "message_id": "very-long-private-resource-id",
        "checks": {"text_match": False, "recipient": "guest@example.com"},
    })
    assert sanitized.get("text", "[redacted]") == "[redacted]"
    assert sanitized["expected_text_hash"] == "abc123"
    assert sanitized["message_id"].startswith("<opaque:")
    assert sanitized["checks"].get("recipient", "[redacted]") == "[redacted]"


def test_tool_selection_reconciliation_retries_only_exact_step():
    decision = asyncio.run(reconcile_failed_step(
        {"id": "run-1", "error_category": "tool_selection"},
        {
            "id": "step-2", "service": "sheets", "operation": "write",
            "read_only": False, "error_category": "tool_selection",
            "input_data": {"allowed_tools": ["write_google_sheet"]},
            "output_data": {"tool_executions": []},
        },
    ))
    assert decision.state == "safe_to_retry"
    assert decision.resume_step_id == "step-2"
    assert decision.reason_code == "no_write_tool_attempted"


def test_reconciliation_marks_verified_write_complete(monkeypatch):
    async def verified(*args, **kwargs):
        return VerificationOutcome(True, "verified", evidence={"match": True})

    monkeypatch.setattr(
        "app.runs.reconciliation.verify_executions_detailed", verified,
    )
    decision = asyncio.run(reconcile_failed_step(
        {"id": "run-1"},
        {
            "id": "step-2", "service": "calendar", "operation": "create",
            "read_only": False,
            "input_data": {"allowed_tools": ["create_calendar_event"]},
            "output_data": {"tool_executions": [{
                "tool": "create_calendar_event", "arguments": {},
                "result": {"id": "event-1"},
            }]},
        },
    ))
    assert decision.state == "already_completed"


def test_uncertain_sheet_append_requires_manual_reconciliation(monkeypatch):
    async def mismatch(*args, **kwargs):
        return VerificationOutcome(
            False, "mismatch", category="postcondition_failure",
            boundary="expected_state_mismatch",
        )

    monkeypatch.setattr(
        "app.runs.reconciliation.verify_executions_detailed", mismatch,
    )
    decision = asyncio.run(reconcile_failed_step(
        {"id": "run-1"},
        {
            "id": "step-2", "service": "sheets", "operation": "append",
            "read_only": False,
            "input_data": {"allowed_tools": ["append_to_google_sheet"]},
            "output_data": {"tool_executions": [{
                "tool": "append_to_google_sheet", "arguments": {},
                "result": {"spreadsheetId": "sheet-1"},
            }]},
        },
    ))
    assert decision.state == "manual_required"
    assert decision.reason_code == "sheet_append_cannot_be_proven"
