"""Trusted, zero-tool answers for questions about the product itself.

The classifier is deterministic because product identity and executable tool
capabilities are security boundaries.  The response is assembled from the
registered operations, so it is not a single brittle canned answer.
"""

import re
from collections.abc import Mapping
from pathlib import Path

from app.okf.loader import load_bundle


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

_SCOPE_CHAT_PATTERNS = (
    r"^(?:hi|hello|hey|thanks|thank you|ok|okay)[.!? ]*$",
    r"^(?:what|why|how|huh)[?!. ]*$",
    r"^(?:can you help|help me)[?!. ]*$",
)
_GUIDANCE_PATTERNS = (
    r"\bhow (?:do|can|should|would) (?:i|you)\b",
    r"\bexplain (?:how|what)\b", r"\bguide me\b", r"\bwhat is\b",
)
_ACTION_PATTERN = (
    r"\b(search|find|list|show|get|read|send|reply|create|make|build|write|append|"
    r"update|modify|share|invite|schedule|delete|trash|move|complete|cancel|check)\b"
)
_WORKSPACE_TERMS = tuple(SERVICE_LABELS) + (
    "email", "mail", "spreadsheet", "document", "meeting", "event",
    "google workspace", "workspace",
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


def classify_workspace_intent(message: str, detected_services: list[str]) -> tuple[str, dict]:
    """Route bounded Workspace conversation without offering global chat."""
    text = " ".join(message.casefold().split())
    product = classify_informational_intent(message)
    evidence = {"product_intent": product, "services": detected_services}
    def routed(kind: str, basis: str, confidence: str = "high") -> tuple[str, dict]:
        return kind, {**evidence, "basis": basis, "confidence": confidence,
                      "ambiguous": kind == "ambiguous"}
    if product:
        return routed("product_information", "trusted product-information pattern")
    if any(re.search(pattern, text) for pattern in _SCOPE_CHAT_PATTERNS):
        return routed("scope_chat", "bounded conversational pattern")
    workspace_context = bool(detected_services) or any(term in text for term in _WORKSPACE_TERMS)
    action = bool(re.search(_ACTION_PATTERN, text))
    guidance = any(re.search(pattern, text) for pattern in _GUIDANCE_PATTERNS)
    if workspace_context and guidance:
        return routed("workspace_guidance", "Workspace entity plus guidance wording")
    if workspace_context and action:
        return routed("workspace_action", "Workspace entity plus actionable operation")
    if workspace_context:
        return routed("ambiguous", "Workspace entity without a supported action", "medium")
    return routed("out_of_scope", "no Workspace entity or product intent")


def capability_catalog(operation_tools: Mapping[tuple[str, str], list[str]]) -> dict[str, list[str]]:
    """Derive user-visible operations from the same registry used by the planner."""
    catalog: dict[str, list[str]] = {}
    for service, operation in operation_tools:
        if service not in SERVICE_LABELS:
            continue
        catalog.setdefault(service, []).append(OPERATION_LABELS.get(operation, operation))
    return {service: sorted(set(operations)) for service, operations in catalog.items()}


def approved_okf_capability_sources() -> list[str]:
    """Return only human-approved capability concepts; drafts cannot guide responses."""
    root = Path(__file__).resolve().parents[2] / "knowledge"
    documents, errors = load_bundle(root, enforce_governance=True)
    if errors:
        return []
    return [item["id"] for item in documents
            if item["trusted"] and item["concept_type"] == "capability"]


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


def workspace_chat_answer(
    message: str, intent: str, catalog: Mapping[str, list[str]],
    okf_sources: list[str] | None = None,
) -> str:
    """Answer locally from trusted metadata; never invoke Google, RAG, or an LLM."""
    text = " ".join(message.casefold().split())
    if intent == "scope_chat":
        if re.fullmatch(r"(?:what|why|how|huh)[?!. ]*", text):
            return ("I may be missing the Workspace action you mean. Tell me the Google "
                    "service and outcome—for example, ‘search Gmail’ or ‘create a Calendar event’.")
        return ("Hello. I’m the Google Workspace Agent. I can help with Workspace "
                "operations and guidance; tell me the service and outcome you want.")
    if intent == "workspace_guidance":
        focused = [service for service in SERVICE_LABELS if service in text]
        descriptions = [
            f"{SERVICE_LABELS[s]} supports {', '.join(catalog.get(s, []))}"
            for s in focused if catalog.get(s)
        ]
        prefix = (("For this agent, " + "; ".join(descriptions) + ". ")
                  if descriptions else "I can explain supported Google Workspace operations. ")
        return prefix + ("Ask with the resource, desired outcome, and any recipient, time, "
                         "or destination. I will request missing details and confirmation "
                         "for high-risk writes. Guidance is bounded by the registered tool "
                         f"catalog and {len(okf_sources or [])} approved capability policies.")
    if intent == "ambiguous":
        return ("I recognized a Google Workspace topic but not a concrete supported action. "
                "Please say which service, resource, and outcome you want.")
    return ("I’m limited to Google Workspace operations and related guidance, not general "
            "chat. I can help with Gmail, Drive, Docs, Sheets, Calendar/Meet, Chat, "
            "Contacts, and Tasks.")
