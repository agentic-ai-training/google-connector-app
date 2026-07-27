"""Pure write-operation contracts shared by planning, execution, and verification."""

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
        "chat", "send", ("send_chat_message",), "all",
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
