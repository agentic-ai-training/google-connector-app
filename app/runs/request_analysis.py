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
    # A bare "space" is ordinary language too ("engineering space"). Chat is
    # recognized through explicit Chat wording/resource syntax below.
    "chat": ("chat",),
    "contacts": ("contact", "contacts", "people"),
    "meet": ("meet", "conference", "video call"),
}

COMPOSITION_PATTERN = re.compile(
    r"\b(draft|compose|write|rewrite|revise|shorten|shorter|expand|longer|"
    r"polish|brainstorm|outline|summarize|summarise|application|essay|roadmap|"
    r"cover letter|pointers?|talking points?|bullet points?|message body|"
    r"email body)\b"
)
COMPOSITION_CREATION_PATTERN = re.compile(
    r"\b(?:create|make|generate|prepare|produce)\b.{0,50}\b"
    r"(?:paragraph|prose|letter|memo|caption|script|message|draft|content)\b"
)
LOCAL_PROJECT_FILE_PATTERN = re.compile(
    r"(?:^|[\s`'\"/(])[\w.-]+\."
    r"(?:md|markdown|py|js|jsx|ts|tsx|java|go|rs|toml|ya?ml|json|env|ini|cfg)"
    r"\b|"
    r"\b(?:repository|repo|codebase|source code|project file|git branch|"
    r"github pull request)\b",
    re.IGNORECASE,
)
EXPLICIT_GOOGLE_RESOURCE_PATTERN = re.compile(
    r"\bgoogle\s+(?:docs?|drive|sheets?|workspace)\b",
    re.IGNORECASE,
)
REFERENCE_PATTERN = re.compile(
    r"\b(it|that|this|them|those|previous|above|earlier|same|former|latter|"
    r"one|ones|version)\b|"
    r"\b(?:the|that|this|previous|above|earlier|same)\s+"
    r"(?:draft|result|link|content|idea|ideas|point|points|paragraph|message)\b"
)
CONTEXTUAL_ACTION_PATTERN = re.compile(
    r"\b(send|sent|email|chat|post|share|use|put|add|remove|change|rewrite|revise|shorten|"
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
DELIVERY_CHANNEL_PATTERNS = {
    "gmail": re.compile(r"\b(?:gmail|e-?mail|mails?)\b", re.IGNORECASE),
    "chat": re.compile(
        r"\b(?:google\s+chat|chat\s+message|chat)\b|"
        r"\bsend\s*(?:on\s+)?(?:google\s+)?chat\b",
        re.IGNORECASE,
    ),
}
LOCAL_ANTECEDENT_PATTERN = re.compile(
    r"\b(create|draft|write|compose|find|get|make|build|prepare)\b.+"
    r"(?:\band\b|\bthen\b|[,.;])\s*(?:then\s+)?"
    r"(?:send|share|email|post|use|put|add|change|rewrite|revise|shorten|"
    r"expand|summarize|summarise|format|turn|convert|make)\s+"
    r"(?:it|that|this|them|those)\b"
)
LOCAL_RESOURCE_ANTECEDENT_PATTERN = re.compile(
    r"\b(?:fetch|find|get|read|list|search)\b.+?"
    r"(?:\band\b|\bthen\b|[,.;])\s*(?:then\s+)?"
    r"(?:send|share|email|post|use|put|add|change|rewrite|revise|shorten|"
    r"expand|summarize|summarise|format|turn|convert|make)\s+"
    r"(?:the\s+)?same\s+(?:mail|email|message|file|document|event|task|content)\b"
)
DEFERRED_EXTERNAL_WRITE_PATTERN = re.compile(
    r"\b(?:wait|hold)\b.{0,100}\b(?:until|for)\b.{0,100}"
    r"\b(?:next|later|another)\s+(?:command|instruction|message)\b|"
    r"\b(?:do\s+not|don't)\s+(?:send|share|post|email)\b.{0,100}"
    r"\b(?:until|unless)\b",
)
GMAIL_COPY_SOURCE_PATTERN = re.compile(
    r"\b(?:last|latest|most\s+recent|previous)\s+"
    r"(?:mail|email|message)\s+(?:that\s+)?(?:you\s+)?sent\s+to\b|"
    r"\b(?:fetch|find|get|read|search)\b.{0,120}"
    r"\b(?:last|latest|most\s+recent|previous)\s+(?:mail|email|message)\b",
)
GMAIL_COPY_TRANSFER_PATTERN = re.compile(
    r"\b(?:send|forward|copy)\b.{0,120}"
    r"\b(?:same\s+)?(?:mail|email|message|it)\b|"
    r"\b(?:send|forward|copy)\s+(?:it|that|this)\s+to\b",
)


@dataclass(frozen=True)
class RequestStatementAnalysis:
    normalized_text: str
    explicit_services: list[str] = field(default_factory=list)
    composition_requested: bool = False
    contextual_reference: bool = False
    service_only: str | None = None
    current_authorizes_external_write: bool = False
    deferred_external_write: bool = False
    gmail_copy_requested: bool = False
    email_recipients: list[str] = field(default_factory=list)
    delivery_channels: list[str] = field(default_factory=list)
    chat_destination_emails: list[str] = field(default_factory=list)

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
            "deferred_external_write": self.deferred_external_write,
            "gmail_copy_requested": self.gmail_copy_requested,
            "email_recipients": self.email_recipients,
            "delivery_channels": self.delivery_channels,
            "chat_destination_emails": self.chat_destination_emails,
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
            "deferred_external_write": self.deferred_external_write,
            "gmail_copy_requested": self.gmail_copy_requested,
            "email_recipient_count": len(self.email_recipients),
            "delivery_channels": self.delivery_channels,
            "chat_destination_email_count": len(self.chat_destination_emails),
        }


def _service_only(normalized_text: str) -> str | None:
    return next((
        canonical for canonical, aliases in SERVICES.items()
        if normalized_text == canonical or normalized_text in aliases
    ), None)


def is_local_project_request(message: str) -> bool:
    """Distinguish repository/filesystem work from similarly named Google resources."""
    return bool(
        LOCAL_PROJECT_FILE_PATTERN.search(message)
        and not EXPLICIT_GOOGLE_RESOURCE_PATTERN.search(message)
    )


def analyze_request_statement(message: str) -> RequestStatementAnalysis:
    """Analyze the current statement only; never load conversation history here."""
    normalized = " ".join(message.casefold().strip().split())
    # Users commonly concatenate the command and channel ("sendchat"). Treat it
    # as the natural phrase "send chat" before applying word-boundary policies.
    normalized = re.sub(
        r"\bsend\s*(?:on\s+)?(?:google\s+)?chat\b",
        "send chat",
        normalized,
    )
    # Domains such as ``@gmail.com`` identify a recipient, not the user's intended
    # delivery service. Remove addresses before scanning service nouns so an
    # explicit Google Chat request cannot silently acquire a Gmail mutation.
    service_text = EMAIL_PATTERN.sub(" ", normalized)
    services = [
        service for service, aliases in SERVICES.items()
        if any(
            re.search(rf"\b{re.escape(alias)}\b", service_text)
            for alias in aliases
        )
    ]
    if is_local_project_request(message):
        services = []
    delivery_channels = [
        channel for channel, pattern in DELIVERY_CHANNEL_PATTERNS.items()
        if pattern.search(service_text)
    ]
    recipients = list(dict.fromkeys(EMAIL_PATTERN.findall(message)))
    deferred_external_write = bool(
        DEFERRED_EXTERNAL_WRITE_PATTERN.search(normalized)
    )
    gmail_copy_requested = bool(
        "gmail" in services
        and len(recipients) >= 2
        and GMAIL_COPY_SOURCE_PATTERN.search(normalized)
        and (
            GMAIL_COPY_TRANSFER_PATTERN.search(normalized)
            or len(re.findall(r"\b(?:send|forward|copy)\b", normalized)) >= 2
        )
    )
    chat_destinations = []
    chat_matches = list(re.finditer(r"\b(?:google\s+chat|chat)\b", normalized))
    for recipient in recipients:
        recipient_match = re.search(re.escape(recipient.casefold()), normalized)
        if not recipient_match:
            continue
        nearest = min(
            chat_matches,
            key=lambda match: abs(match.start() - recipient_match.start()),
            default=None,
        )
        if not nearest:
            continue
        start, end = sorted((nearest.end(), recipient_match.start()))
        between = normalized[start:end]
        if (
            len(between) <= 100
            and not re.search(
                r"\b(?:calendar|calender|event|meet|meeting|invite|schedule)\b",
                between,
            )
        ):
            chat_destinations.append(recipient)
    contextual = bool(
        (
            REFERENCE_PATTERN.search(normalized)
            and CONTEXTUAL_ACTION_PATTERN.search(normalized)
        )
        or re.search(r"\b(what about|instead|do the same|go ahead)\b", normalized)
    )
    if contextual and (
        LOCAL_ANTECEDENT_PATTERN.search(normalized)
        or LOCAL_RESOURCE_ANTECEDENT_PATTERN.search(normalized)
        or gmail_copy_requested
        or deferred_external_write
    ):
        contextual = False
    composition = bool(
        COMPOSITION_PATTERN.search(normalized)
        or COMPOSITION_CREATION_PATTERN.search(normalized)
    )
    if (
        contextual
        and composition
        and re.search(
            r"\b(?:and|then)\s+send\s+(?:(?:on\s+)?chat\s+)?"
            r"(?:it|that|this)\b",
            normalized,
        )
    ):
        contextual = False
    external_write = not deferred_external_write and bool(
        EXTERNAL_WRITE_PATTERN.search(normalized)
        or EMAIL_WRITE_PATTERN.search(message)
        or (
            "chat" in delivery_channels
            and bool(re.search(r"\b(?:send|chat|post|message)\b", normalized))
            and bool(recipients)
        )
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
        deferred_external_write=deferred_external_write,
        gmail_copy_requested=gmail_copy_requested,
        email_recipients=recipients,
        delivery_channels=delivery_channels,
        chat_destination_emails=chat_destinations,
    )
