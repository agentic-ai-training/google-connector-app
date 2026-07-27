"""Structured analysis of every current user statement before classification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


SERVICES = {
    "gmail": ("gmail", "email", "emails", "emials", "mail", "mails", "maisl"),
    "calendar": ("calendar", "event", "schedule", "invite", "meeting"),
    "drive": ("drive", "file", "files", "folder", "folders", "share"),
    "docs": ("doc", "docs", "document", "documents"),
    "sheets": ("sheet", "spreadsheet", "table", "rows"),
    "tasks": ("task", "tasks", "todo"),
    "chat": ("chat", "space"),
    "contacts": ("contact", "contacts", "people"),
    "meet": ("meet", "conference", "video call"),
}

COMPOSITION_PATTERN = re.compile(
    r"\b(draft|compose|write|rewrite|revise|shorten|shorter|expand|longer|"
    r"polish|brainstorm|outline|summarize|summarise|application|essay|roadmap|"
    r"pointers?|talking points?|bullet points?|message body|email body)\b"
)
REFERENCE_PATTERN = re.compile(
    r"\b(it|that|this|them|those|previous|above|earlier|same|former|latter|"
    r"one|ones|version)\b|"
    r"\b(?:the|that|this|previous|above|earlier|same)\s+"
    r"(?:draft|result|link|content|idea|ideas|point|points)\b"
)
CONTEXTUAL_ACTION_PATTERN = re.compile(
    r"\b(send|email|post|share|use|put|add|remove|change|rewrite|revise|shorten|"
    r"expand|summarize|summarise|format|turn|convert|make|continue|finish)\b"
)
EXTERNAL_WRITE_PATTERN = re.compile(
    r"\b(send|reply|post|share|invite|schedule|delete|trash|cancel|"
    r"publish|transfer)\b"
)
EMAIL_WRITE_PATTERN = re.compile(
    r"\bemail\s+(?:it|them|this|that|to)\b|"
    r"(?:^|\bplease\s+)email\s+[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE,
)
LOCAL_ANTECEDENT_PATTERN = re.compile(
    r"\b(create|draft|write|compose|find|get|make|build|prepare)\b.+"
    r"(?:\band\b|\bthen\b|[,.;])\s*(?:then\s+)?"
    r"(?:send|share|email|post|use|put|add|change|rewrite|revise|shorten|"
    r"expand|summarize|summarise|format|turn|convert|make)\s+"
    r"(?:it|that|this|them|those)\b"
)


@dataclass(frozen=True)
class RequestStatementAnalysis:
    normalized_text: str
    explicit_services: list[str] = field(default_factory=list)
    composition_requested: bool = False
    contextual_reference: bool = False
    service_only: str | None = None
    current_authorizes_external_write: bool = False
    email_recipients: list[str] = field(default_factory=list)

    def classifier_input(self) -> dict:
        """Structured current-turn facts that classification is required to consume."""
        return {
            "version": "request-statement-v1",
            "normalized_text": self.normalized_text,
            "explicit_services": self.explicit_services,
            "composition_requested": self.composition_requested,
            "contextual_reference": self.contextual_reference,
            "service_only": self.service_only,
            "current_authorizes_external_write": (
                self.current_authorizes_external_write
            ),
            "email_recipients": self.email_recipients,
        }

    def diagnostics(self) -> dict:
        """Content-free evidence that the analyzer ran before classification."""
        return {
            "version": "request-statement-v1",
            "analyzed": True,
            "explicit_services": self.explicit_services,
            "composition_requested": self.composition_requested,
            "contextual_reference": self.contextual_reference,
            "service_only": self.service_only,
            "current_authorizes_external_write": (
                self.current_authorizes_external_write
            ),
            "email_recipient_count": len(self.email_recipients),
        }


def _service_only(normalized_text: str) -> str | None:
    return next((
        canonical for canonical, aliases in SERVICES.items()
        if normalized_text == canonical or normalized_text in aliases
    ), None)


def analyze_request_statement(message: str) -> RequestStatementAnalysis:
    """Analyze the current statement only; never load conversation history here."""
    normalized = " ".join(message.casefold().strip().split())
    services = [
        service for service, aliases in SERVICES.items()
        if any(
            re.search(rf"\b{re.escape(alias)}\b", normalized)
            for alias in aliases
        )
    ]
    contextual = bool(
        (
            REFERENCE_PATTERN.search(normalized)
            and CONTEXTUAL_ACTION_PATTERN.search(normalized)
        )
        or re.search(r"\b(what about|instead|do the same|go ahead)\b", normalized)
    )
    if contextual and LOCAL_ANTECEDENT_PATTERN.search(normalized):
        contextual = False
    composition = bool(COMPOSITION_PATTERN.search(normalized))
    external_write = bool(
        EXTERNAL_WRITE_PATTERN.search(normalized)
        or EMAIL_WRITE_PATTERN.search(message)
        or (
            re.search(r"\b(create|append|update|modify|upload|move|complete)\b",
                      normalized)
            and any(
                service in services
                for service in (
                    "calendar", "drive", "docs", "sheets", "tasks", "meet",
                )
            )
        )
    )
    return RequestStatementAnalysis(
        normalized_text=normalized,
        explicit_services=services,
        composition_requested=composition,
        contextual_reference=contextual,
        service_only=_service_only(normalized),
        current_authorizes_external_write=external_write,
        email_recipients=EMAIL_PATTERN.findall(message),
    )
