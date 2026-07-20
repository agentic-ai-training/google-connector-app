"""Trusted, zero-tool answers for questions about the product itself.

The classifier is deterministic because product identity and executable tool
capabilities are security boundaries.  The response is assembled from the
registered operations, so it is not a single brittle canned answer.
"""

import re
from collections.abc import Mapping


PRODUCT_NAME = "Google Workspace Agent"

SERVICE_LABELS = {
    "gmail": "Gmail",
    "calendar": "Google Calendar",
    "drive": "Google Drive",
    "docs": "Google Docs",
    "sheets": "Google Sheets",
    "tasks": "Google Tasks",
    "chat": "Google Chat",
    "contacts": "Google Contacts",
    "meet": "Google Meet",
}

OPERATION_LABELS = {
    "search": "search",
    "get": "inspect",
    "read": "read",
    "list": "list",
    "send": "send",
    "reply": "reply",
    "label": "label",
    "trash": "trash",
    "create": "create",
    "create_and_write": "create and populate",
    "write": "write",
    "append": "append",
    "update": "update",
    "delete": "delete",
    "availability": "check availability",
    "share": "share",
    "move": "move",
    "upload": "upload",
    "complete": "complete",
    "list_spaces": "list spaces",
    "participants": "list participants",
    "conferences": "list conference records",
}

_IDENTITY_PATTERNS = (
    r"\bwhat(?:'s| is) your name\b",
    r"\bwho are you\b",
    r"\bintroduce yourself\b",
    r"\byour identity\b",
)
_CAPABILITY_PATTERNS = (
    r"\bwhat can you do\b",
    r"\bwhat (?:else )?can you (?:do|help with)\b",
    r"\b(?:your|what are your) capabilit(?:y|ies)\b",
    r"\bwhat (?:operations|services|tools) (?:can|do) you\b",
    r"\bwhich (?:operations|services|tools)\b",
    r"\bcan you only (?:do|use|work with)\b",
    r"\bother than .+ what about\b",
    r"\bwhat about (?:google )?(?:meet|gmail|drive|calendar|chat|docs|sheets|tasks|contacts)\b",
)


def classify_informational_intent(message: str) -> str | None:
    """Return identity/capabilities/combined for product-information questions."""
    text = " ".join(message.casefold().split())
    identity = any(re.search(pattern, text) for pattern in _IDENTITY_PATTERNS)
    capabilities = any(re.search(pattern, text) for pattern in _CAPABILITY_PATTERNS)
    if identity and capabilities:
        return "identity_and_capabilities"
    if identity:
        return "identity"
    if capabilities:
        return "capabilities"
    return None


def capability_catalog(operation_tools: Mapping[tuple[str, str], list[str]]) -> dict[str, list[str]]:
    """Derive user-visible operations from the same registry used by the planner."""
    catalog: dict[str, list[str]] = {}
    for service, operation in operation_tools:
        if service not in SERVICE_LABELS:
            continue
        catalog.setdefault(service, []).append(OPERATION_LABELS.get(operation, operation))
    return {service: sorted(set(operations)) for service, operations in catalog.items()}


def informational_answer(
    message: str,
    intent: str,
    catalog: Mapping[str, list[str]],
) -> str:
    """Compose an authoritative answer, optionally focused on mentioned services."""
    text = " ".join(message.casefold().split())
    parts = []
    if intent in {"identity", "identity_and_capabilities"}:
        parts.append(f"My name is {PRODUCT_NAME}.")
    if intent in {"capabilities", "identity_and_capabilities"}:
        focused = [
            service for service, label in SERVICE_LABELS.items()
            if re.search(rf"\b{re.escape(service)}\b", text)
            or re.search(rf"\b{re.escape(label.casefold())}\b", text)
        ]
        selected = focused or [service for service in SERVICE_LABELS if service in catalog]
        descriptions = [
            f"{SERVICE_LABELS[service]} ({', '.join(catalog.get(service, []))})"
            for service in selected if catalog.get(service)
        ]
        if focused:
            parts.append("Yes. I can work with " + "; ".join(descriptions) + ".")
        else:
            parts.append("I can work with " + "; ".join(descriptions) + ".")
        parts.append(
            "I use live Google APIs for current Workspace data, and I ask for confirmation "
            "before high-risk external writes unless you explicitly waive it."
        )
    return " ".join(parts)
