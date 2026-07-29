"""Pure write-operation contracts shared by planning, execution, and verification."""

import re
from dataclasses import dataclass
from typing import Literal


CompletionMode = Literal["any", "all", "ordered"]


@dataclass(frozen=True)
class WriteContract:
    service: str
    operation: str
    required_tools: tuple[str, ...]
    completion_mode: CompletionMode


WRITE_CONTRACTS: dict[tuple[str, str], WriteContract] = {
    ("gmail", "send"): WriteContract("gmail", "send", ("send_gmail",), "all"),
    ("gmail", "reply"): WriteContract("gmail", "reply", ("reply_gmail",), "all"),
    ("gmail", "label"): WriteContract("gmail", "label", ("label_gmail",), "all"),
    ("gmail", "trash"): WriteContract("gmail", "trash", ("trash_gmail",), "all"),
    ("calendar", "create"): WriteContract(
        "calendar", "create", ("create_calendar_event",), "all",
    ),
    ("calendar", "update"): WriteContract(
        "calendar", "update", ("update_calendar_event",), "all",
    ),
    ("calendar", "delete"): WriteContract(
        "calendar", "delete", ("delete_calendar_event",), "all",
    ),
    ("drive", "upload"): WriteContract(
        "drive", "upload", ("upload_drive_file",), "all",
    ),
    ("drive", "share"): WriteContract(
        "drive", "share", ("share_drive_file",), "all",
    ),
    ("drive", "move"): WriteContract("drive", "move", ("move_drive_file",), "all"),
    ("drive", "trash"): WriteContract(
        "drive", "trash", ("trash_drive_file",), "all",
    ),
    ("docs", "create"): WriteContract(
        "docs", "create", ("create_google_doc",), "all",
    ),
    ("docs", "append"): WriteContract(
        "docs", "append", ("append_to_google_doc",), "all",
    ),
    ("sheets", "create"): WriteContract(
        "sheets", "create", ("create_google_sheet",), "all",
    ),
    ("sheets", "write"): WriteContract(
        "sheets", "write", ("write_google_sheet",), "all",
    ),
    ("sheets", "append"): WriteContract(
        "sheets", "append", ("append_to_google_sheet",), "all",
    ),
    ("sheets", "create_and_write"): WriteContract(
        "sheets", "create_and_write",
        ("create_google_sheet", "write_google_sheet"), "ordered",
    ),
    ("tasks", "create"): WriteContract("tasks", "create", ("create_task",), "all"),
    ("tasks", "complete"): WriteContract(
        "tasks", "complete", ("complete_task",), "all",
    ),
    ("chat", "send"): WriteContract(
        "chat", "send",
        ("resolve_chat_destination", "send_chat_message"), "ordered",
    ),
    ("meet", "create"): WriteContract(
        "meet", "create", ("create_meet_space",), "all",
    ),
}

WRITE_TOOLS = frozenset(
    tool for contract in WRITE_CONTRACTS.values() for tool in contract.required_tools
)


def write_contract_for(
    service: str | None,
    operation: str | None,
    allowed_tools: list[str] | tuple[str, ...] | None = None,
) -> WriteContract | None:
    contract = WRITE_CONTRACTS.get((service or "", operation or ""))
    if not contract:
        return None
    if allowed_tools is None:
        return contract
    allowed = set(allowed_tools)
    if not set(contract.required_tools).issubset(allowed):
        return None
    return contract


def _failed(execution: dict) -> bool:
    result = execution.get("result")
    return (
        not isinstance(result, dict)
        or bool(result.get("error"))
        or result.get("success") is False
    )


def attempted_required_tools(
    contract: WriteContract, executions: list[dict],
) -> list[str]:
    required = set(contract.required_tools)
    return [
        str(item.get("tool")) for item in executions
        if item.get("tool") in required
    ]


def successful_required_tools(
    contract: WriteContract, executions: list[dict],
) -> list[str]:
    required = set(contract.required_tools)
    return [
        str(item.get("tool")) for item in executions
        if item.get("tool") in required and not _failed(item)
    ]


def next_ordered_required_tool(
    contract: WriteContract | None, executions: list[dict],
) -> str | None:
    """Return the only required write currently allowed by an ordered contract."""
    if contract is None or contract.completion_mode != "ordered":
        return None
    successful = successful_required_tools(contract, executions)
    for index, name in enumerate(contract.required_tools):
        if index >= len(successful) or successful[index] != name:
            return name
    return None


def bind_ordered_output_lineage(
    contract: WriteContract | None, call: dict, executions: list[dict],
) -> tuple[dict, dict]:
    """Bind downstream write identifiers to verified upstream tool output.

    The model may select dependent tools in one response, before it has observed
    the create result. The executor, not the model, owns resource-ID lineage.
    """
    if contract is None:
        return call, {}
    if (
        contract.service == "chat"
        and contract.operation == "send"
        and call.get("name") == "send_chat_message"
    ):
        resolved = next((
            item.get("result") for item in reversed(executions)
            if item.get("tool") == "resolve_chat_destination"
            and isinstance(item.get("result"), dict)
            and re.fullmatch(r"spaces/[^/]+", str(item["result"].get("name") or ""))
            and not item["result"].get("error")
        ), None)
        if not resolved:
            return call, {}
        updated = {**call, "args": {
            **(call.get("args") or {}),
            "space_id": resolved["name"],
        }}
        return updated, {
            "lineage_source_tool": "resolve_chat_destination",
            "lineage_target_tool": "send_chat_message",
            "lineage_field": "space_id",
            "lineage_bound": True,
        }
    if (
        contract.service != "sheets"
        or contract.operation != "create_and_write"
        or call.get("name") != "write_google_sheet"
    ):
        return call, {}
    created = next((
        item.get("result") for item in reversed(executions)
        if item.get("tool") == "create_google_sheet"
        and isinstance(item.get("result"), dict)
        and item["result"].get("spreadsheetId")
        and not item["result"].get("error")
    ), None)
    if not created:
        return call, {}
    updated = {**call, "args": {
        **(call.get("args") or {}),
        "spreadsheet_id": created["spreadsheetId"],
    }}
    return updated, {
        "lineage_source_tool": "create_google_sheet",
        "lineage_target_tool": "write_google_sheet",
        "lineage_field": "spreadsheet_id",
        "lineage_bound": True,
    }


def missing_required_tools(
    contract: WriteContract, executions: list[dict],
) -> list[str]:
    successful = set(successful_required_tools(contract, executions))
    return [tool for tool in contract.required_tools if tool not in successful]


def contract_satisfied(contract: WriteContract, executions: list[dict]) -> bool:
    successful = successful_required_tools(contract, executions)
    if contract.completion_mode == "any":
        return bool(successful)
    if contract.completion_mode == "all":
        return set(contract.required_tools).issubset(successful)
    cursor = iter(successful)
    return all(any(item == required for item in cursor) for required in contract.required_tools)
