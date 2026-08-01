import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.runs.schemas import ExecutionPlan, PlanStep
from app.runs.informational import (
    capability_catalog,
    classify_workspace_intent,
    approved_okf_capability_sources,
)
from app.tools.contracts import WRITE_TOOLS, write_contract_for
from app.tools.registry import registered_tool_names
from app.runs.request_analysis import (
    SERVICES,
    RequestStatementAnalysis,
    analyze_request_statement,
    is_local_project_request,
)
from app.runs.content_policy import analyze_content_request
GMAIL_DELIVERY_PATTERN = re.compile(
    r"\b(send|reply|mail it|mail them)\b|"
    r"\bemail\s+(it|them|this|that|the|my|our|[A-Za-z0-9._%+-]+@)\b"
)
CHAT_DELIVERY_PATTERN = re.compile(
    r"\b(send|post|message them|chat them)\b|"
    r"\bsend\s*(?:on\s+)?(?:google\s+)?chat\b"
)

WRITE_PATTERNS = (
    r"\bsend\b", r"\breply\b", r"\b(?:create|make|add|set|put)\b", r"\bwrite\b", r"\bappend\b",
    r"\bupdate\b", r"\bmodify\b", r"\bshare\b", r"\binvite\b", r"\bschedule\b",
    r"\bdelete\b", r"\btrash\b", r"\bmove\b", r"\bcomplete\b", r"\bcancel\b",
)
HIGH_RISK_PATTERNS = (
    r"\bsend\b.*\b(email|mail|message|chat)\b",
    r"\b(email|mail|message|chat)\b.*\bsend\b",
    r"\b(reply|invite|share|publish|delete|trash|cancel|transfer)\b",
    r"\b(schedule|create)\b.*\b(event|meeting|invite)\b",
    r"\b(event|meeting|invite)\b.*\b(schedule|create)\b",
    r"\bbulk\b", r"\beveryone\b", r"\bpublic(?:ly)?\b",
)
APPROVAL_OPT_OUT = (
    "without asking", "do not ask", "don't ask", "no confirmation",
    "without confirmation", "proceed automatically",
)
SEMANTIC_TERMS = (
    "similar", "related", "concept", "summarize documents", "across documents",
    "historical", "remember", "previous discussion",
)
LIVE_TERMS = (
    "latest", "last email", "recent email", "create", "send", "schedule", "update",
    "delete", "share", "list", "get", "read",
)

# Reads that produce identifiers/data come first. Artifact-producing services then
# run before delivery services. The conservative ordering deliberately serializes
# writes until the evaluator proves that a pair is safe to parallelize.
SERVICE_ORDER = {
    "contacts": 10, "composition": 15, "gmail": 20, "drive": 30,
    "docs": 40, "sheets": 50,
    "tasks": 60, "chat": 70, "calendar": 80, "meet": 90, "general": 100,
}

SERVICE_POSTCONDITIONS = {
    "composition": [
        "The requested content is complete and satisfies the stated format and audience",
        "No external Google mutation is claimed by the composition step",
    ],
    "gmail": ["Every Gmail write has a message identifier and correct recipient"],
    "drive": ["Every created or shared Drive artifact has an ID, URL, and sharing state"],
    "docs": ["Every Docs write has a document ID and verified document URL"],
    "sheets": ["Every Sheets write has a spreadsheet ID and expected row/content evidence"],
    "chat": ["Every Chat write has a message resource ID and correct destination space"],
    "calendar": ["Every event has the requested timezone, attendee state, and event ID"],
    "meet": ["Every Meet operation returns a space/conference ID and meeting URI"],
    "tasks": ["Every Tasks write has a task ID and verified completion state"],
    "contacts": ["Contact results retain stable identifiers and matched addresses"],
}

SERVICE_OPERATION_PATTERNS = {
    "gmail": [("message_count",
               r"\b(?:count|how many|number of)\b.{0,100}\b(?:mail|email|message)s?\b"),
              ("sender_count",
               r"\b(?:count|how many)\b.{0,100}\b(?:senders?|people|persons?)\b|"
               r"\b(?:senders?|people|persons?)\b.{0,100}\b(?:count|how many)\b"),
              ("trash", r"\b(trash|delete)\b"),
              ("label", r"\blabel\b"),
              ("reply", r"\brepl(?:y|ies)\b"), ("send", r"\bsend\b"),
              ("search", r"\b(search|find|latest|recent|last|get|read|list)\b")],
    "calendar": [("delete", r"\b(delete|cancel)\b"), ("update", r"\b(update|move|reschedule)\b"),
                 ("create", r"\b(create|make|add|set|put|schedule|invite|book)\b"),
                 ("availability", r"\b(available|availability|free|busy)\b"),
                 ("list", r"\b(list|show|get|find)\b")],
    "drive": [("trash", r"\b(delete|trash)\b"), ("share", r"\bshare\b"), ("move", r"\bmove\b"),
              ("upload", r"\bupload\b"), ("search", r"\b(search|find|list)\b"),
              ("get", r"\b(get|read|link)\b")],
    "docs": [("append", r"\bappend\b"), ("create", r"\b(create|write|draft)\b"),
             ("read", r"\b(read|get|summarize|find)\b")],
    "sheets": [("append", r"\bappend\b"),
               ("create_and_write", r"\b(create|make|build|populate)\b"),
               ("write", r"\b(write|update|fill)\b"), ("read", r"\b(read|get|list)\b")],
    "tasks": [("complete", r"\b(complete|finish)\b"), ("create", r"\b(create|add)\b"),
              ("list", r"\b(list|show|get)\b")],
    "chat": [("send", r"\b(send|message|post|chat)\b"), ("list_spaces", r"\b(list|show|space)\b")],
    "contacts": [("search", r"\b(search|find|lookup|list)\b"), ("get", r"\bget\b")],
    "meet": [("create", r"\b(create|start|schedule)\b"),
             ("participants", r"\b(participant|attendee|attendance)\b"),
             ("conferences", r"\b(list|recent|conference|record)\b"),
             ("get", r"\b(get|find|lookup)\b")],
}

OPERATION_TOOLS = {
    ("composition", "compose"): [],
    ("gmail", "message_count"): ["count_gmail_messages"],
    ("gmail", "sender_count"): ["count_gmail_senders"],
    ("gmail", "recent_senders"): ["list_recent_gmail_senders"],
    ("gmail", "search"): ["search_gmail", "get_gmail_message", "list_gmail_threads"],
    ("gmail", "send"): ["send_gmail"], ("gmail", "reply"): ["get_gmail_message", "reply_gmail"],
    ("gmail", "label"): ["get_gmail_message", "label_gmail"],
    ("gmail", "trash"): ["get_gmail_message", "trash_gmail"],
    ("calendar", "create"): ["check_calendar_availability", "create_calendar_event", "get_calendar_event"],
    ("calendar", "update"): ["get_calendar_event", "update_calendar_event"],
    ("calendar", "delete"): ["get_calendar_event", "delete_calendar_event"],
    ("calendar", "availability"): ["check_calendar_availability"],
    ("calendar", "list"): ["list_calendar_events", "get_calendar_event"],
    ("drive", "share"): ["get_drive_file", "share_drive_file"],
    ("drive", "trash"): ["get_drive_file", "trash_drive_file"],
    ("drive", "move"): ["get_drive_file", "move_drive_file"],
    ("drive", "upload"): ["upload_drive_file", "get_drive_file"],
    ("drive", "search"): ["search_drive", "get_drive_file"],
    ("drive", "get"): ["search_drive", "get_drive_file"],
    ("docs", "create"): ["create_google_doc", "read_google_doc"],
    ("docs", "append"): ["read_google_doc", "append_to_google_doc"],
    ("docs", "read"): ["read_google_doc"],
    ("sheets", "create_and_write"): ["create_google_sheet", "write_google_sheet",
                                        "append_to_google_sheet", "read_google_sheet"],
    ("sheets", "write"): ["write_google_sheet", "read_google_sheet"],
    ("sheets", "append"): ["append_to_google_sheet", "read_google_sheet"],
    ("sheets", "read"): ["read_google_sheet"],
    ("tasks", "create"): ["create_task", "list_tasks"],
    ("tasks", "complete"): ["list_tasks", "complete_task"],
    ("tasks", "list"): ["list_tasks"],
    ("chat", "send"): [
        "list_chat_spaces", "resolve_chat_destination", "send_chat_message",
    ],
    ("chat", "list_spaces"): ["list_chat_spaces"],
    ("contacts", "search"): ["search_contacts", "get_contact"],
    ("contacts", "get"): ["get_contact"],
    ("meet", "create"): ["create_meet_space", "get_meet_space"],
    ("meet", "participants"): ["list_meet_participants"],
    ("meet", "conferences"): ["list_meet_conferences", "get_meet_space"],
    ("meet", "get"): ["get_meet_space"],
}
READ_OPERATIONS = {"compose", "search", "message_count", "sender_count", "recent_senders", "get", "read", "list",
                   "availability", "list_spaces",
                   "participants", "conferences"}
DEFAULT_READ_OPERATION = {
    "gmail": "search", "calendar": "list", "drive": "search", "docs": "read",
    "sheets": "read", "tasks": "list", "chat": "list_spaces", "contacts": "search",
    "meet": "conferences",
}


def infer_operation(service: str, message: str, write: bool) -> str:
    text = message.lower()
    if service == "composition":
        return "compose"
    if service == "gmail" and re.search(
        r"\b(?:count|how many)\b.{0,100}\b(?:senders?|people|persons?)\b|"
        r"\b(?:senders?|people|persons?)\b.{0,100}\b(?:count|how many)\b",
        text,
    ):
        return "sender_count"
    if service == "gmail" and re.search(
        r"\b(?:people|persons?|senders?|names?)\b.{0,80}\b(?:mail|email)s?\b|"
        r"\b(?:mail|email)s?\b.{0,80}\b(?:people|persons?|senders?|names?)\b",
        text,
    ):
        return "recent_senders"
    anchors = [match.start() for term in SERVICES.get(service, (service,))
               for match in re.finditer(rf"\b{re.escape(term)}\b", text)]
    candidates = []
    for priority, (operation, pattern) in enumerate(
        SERVICE_OPERATION_PATTERNS.get(service, [])
    ):
        for match in re.finditer(pattern, text):
            distance = min((abs(match.start() - anchor) for anchor in anchors), default=0)
            candidates.append((distance, priority, match.start(), operation))
    if candidates:
        return min(candidates)[-1]
    return "execute_and_verify" if write else DEFAULT_READ_OPERATION.get(service, "read")


def infer_operation_sequence(
    service: str,
    message: str,
    write: bool,
    *,
    allow_same_service_expansion: bool = True,
) -> list[str]:
    """Return a conservative read→write sequence for an explicitly chained request.

    A service is usually represented by one step.  When the current turn clearly
    asks for a read followed by a write on that same service, collapsing both into
    the nearest verb silently removes one half of the request.  Only a forward
    read→write pair connected by explicit chaining language is expanded here; all
    ambiguous or write→read cases keep the established single-operation planner.
    """
    default = infer_operation(service, message, write)
    text = message.casefold()
    if not allow_same_service_expansion or not write or not re.search(
        r"\b(?:and(?:\s+then)?|then|after(?:wards)?|same)\b", text,
    ):
        return [default]
    matches: list[tuple[int, int, str]] = []
    for priority, (operation, pattern) in enumerate(
        SERVICE_OPERATION_PATTERNS.get(service, [])
    ):
        for match in re.finditer(pattern, text):
            matches.append((match.start(), priority, operation))
    ordered = []
    for _, _, operation in sorted(matches):
        if operation not in ordered:
            ordered.append(operation)
    reads = [
        (index, operation) for index, operation in enumerate(ordered)
        if operation in READ_OPERATIONS
    ]
    writes = [
        (index, operation) for index, operation in enumerate(ordered)
        if operation not in READ_OPERATIONS
    ]
    if not reads or not writes:
        return [default]
    read_index, read_operation = reads[0]
    write_index, write_operation = writes[-1]
    if read_index >= write_index:
        return [default]
    return [read_operation, write_operation]


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _service_authorization(
    service: str, statement: RequestStatementAnalysis,
) -> dict:
    if service == "composition":
        authorized = statement.composition_requested
        basis = "current_turn_composition"
    elif service == "general":
        authorized = True
        basis = "guarded_workspace_conversation"
    elif service in statement.explicit_services:
        authorized = True
        basis = "current_turn_explicit_service"
    elif (
        service == "gmail"
        and statement.email_recipients
        and "chat" not in statement.delivery_channels
        and re.search(r"\bsend\b", statement.normalized_text)
    ):
        authorized = True
        basis = "current_turn_recipient_delivery"
    else:
        authorized = False
        basis = "no_current_turn_service_authority"
    return {"authorized": authorized, "basis": basis}


TIMEZONE_ALIASES = {
    "utc": "UTC", "gmt": "UTC", "ist": "Asia/Kolkata",
    "india": "Asia/Kolkata", "indian": "Asia/Kolkata",
    "indian standard time": "Asia/Kolkata",
    "est": "America/New_York", "edt": "America/New_York",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "cet": "Europe/Paris",
}


def resolve_timezone(message: str, supplied: str | None = None) -> str | None:
    candidates = [supplied] if supplied else []
    candidates.extend(re.findall(
        r"\b(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|"
        r"Indian|Pacific)/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?\b",
        message,
    ))
    lowered = message.casefold()
    candidates.extend(
        canonical for alias, canonical in TIMEZONE_ALIASES.items()
        if re.search(rf"\b{alias}\b", lowered)
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
            return candidate
        except ZoneInfoNotFoundError:
            continue
    return None


def calendar_create_arguments(
    message: str,
    timezone: str | None,
    recipients: list[str],
    *,
    add_meet: bool,
) -> dict:
    """Project a fully specified natural-language event into bounded arguments."""
    start_match = re.search(
        r"\b(today|tomorrow|tommorow)\b\s+(?:at\s+)?"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
        message,
        re.IGNORECASE,
    )
    duration_match = re.search(
        r"\b(\d+)\s*(minutes?|hours?)\b",
        message,
        re.IGNORECASE,
    )
    if not start_match or not duration_match or not timezone or not recipients:
        return {}
    duration = int(duration_match.group(1))
    if duration <= 0:
        return {}
    if duration_match.group(2).casefold().startswith("hour"):
        duration *= 60
    recipient_list = list(dict.fromkeys(
        item.casefold() for item in recipients if item
    ))
    if not recipient_list:
        return {}
    start_day = start_match.group(1)
    if start_day.casefold() == "tommorow":
        start_day = "tomorrow"
    return {
        "title": f"Meeting with {recipient_list[0]}",
        "start_datetime": f"{start_day} {start_match.group(2)}",
        "duration_minutes": duration,
        "timezone": timezone,
        "attendees": recipient_list,
        "add_meet": add_meet,
    }


def classify_request(
    message: str,
    timezone: str | None = None,
    *,
    authority_message: str | None = None,
    request_analysis: RequestStatementAnalysis | None = None,
) -> dict:
    text = " ".join(message.lower().split())
    statement = request_analysis or analyze_request_statement(
        authority_message or message
    )
    authority_text = statement.normalized_text
    resolved_timezone = resolve_timezone(message, timezone)
    services = [
        service for service, terms in SERVICES.items()
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)
    ]
    authority_services = set(statement.explicit_services)
    # Current-turn analysis is the service authority. This also recognizes
    # normalized forms such as "sendchat" which should not require the prior
    # context string to contain a separately tokenized Chat noun.
    services.extend(authority_services)
    services = list(dict.fromkeys(services))
    if is_local_project_request(authority_message or message):
        services = []
        authority_services.clear()
    if (
        re.search(r"\bsend\b", authority_text)
        and statement.email_recipients
        and "chat" not in statement.delivery_channels
    ):
        services.append("gmail")
        authority_services.add("gmail")
    # Delivery nouns in the current statement are authoritative. Historical
    # context or an address domain may help resolve a recipient, but cannot add a
    # different external-write channel.
    if (
        "chat" in statement.delivery_channels
        and "gmail" not in statement.delivery_channels
    ):
        services = [service for service in services if service != "gmail"]
        authority_services.discard("gmail")
    if (
        "gmail" in statement.delivery_channels
        and "chat" not in statement.delivery_channels
    ):
        services = [service for service in services if service != "chat"]
        authority_services.discard("chat")
    # "Space" is a resource noun for both Chat and Meet. Do not invent a Chat
    # step when the user explicitly asks for a Meet space without mentioning Chat.
    if "meet" in services and "chat" in services and not re.search(r"\bchat\b", text):
        services.remove("chat")
    if "contacts" in services and "gmail" in services and re.search(
        r"\b(?:people|persons?|senders?)\b.{0,50}\b(?:mail|email)", text
    ):
        services.remove("contacts")
    sheet_url_is_drive_link = bool(
        "sheets" in services and "drive" in services
        and (re.search(r"\b(?:drive )?link\b.{0,40}\b(?:sheet|spreadsheet)\b", text)
             or re.search(r"\b(?:sheet|spreadsheet)\b.{0,40}\b(?:drive )?link\b", text)
             or re.search(r"\b(?:its|that|the) (?:verified )?drive link\b", text))
    )
    if sheet_url_is_drive_link:
        services.remove("drive")
    calendar_adds_meet = bool(
        "calendar" in services and "meet" in services
        and re.search(r"\b(schedule|calendar|event|invite|tomorrow|today)\b", text)
    )
    if calendar_adds_meet:
        services.remove("meet")
    # Historical content can supply a referenced body, but it must not classify
    # the current command. For example, a prior paragraph containing the words
    # "your capabilities" must not turn "send the above paragraph on Chat" into
    # a product-capabilities answer. A service-only clarification is the sole
    # case that intentionally needs the combined effective message.
    intent_message = message if statement.service_only else (
        authority_message or message
    )
    intent_kind, intent_evidence = classify_workspace_intent(
        intent_message, services,
    )
    if (
        statement.composition_requested
        and (
            statement.deferred_external_write
            or not statement.explicit_services
        )
        and intent_kind in {"ambiguous", "out_of_scope"}
    ):
        # Bounded composition is supported even without an immediate Workspace
        # mutation. This does not turn the product into an unrestricted factual
        # chatbot: it authorizes creation/transformation only.
        intent_kind = "workspace_action"
        intent_evidence = {
            **intent_evidence,
            "product_intent": None,
            "basis": "structured bounded composition",
            "confidence": "high",
            "ambiguous": False,
        }
    if (
        statement.current_authorizes_external_write
        and authority_services
        and intent_kind in {"ambiguous", "out_of_scope"}
    ):
        intent_kind = "workspace_action"
        intent_evidence = {
            **intent_evidence,
            "product_intent": None,
            "basis": "structured current-turn external-write authority",
            "confidence": "high",
            "ambiguous": False,
        }
    # Context can establish that "it" refers to prior Workspace content, but
    # only the current turn is allowed to choose executable services.
    if statement.contextual_reference:
        services = [
            service for service in services
            if service in authority_services
        ]
    composition_requested = statement.composition_requested
    if intent_kind == "workspace_action" and composition_requested:
        services = [
            service for service in services
            if service in authority_services
        ]
        # Words such as "meeting" may describe the content of a draft rather
        # than authorize a Calendar action. Require an explicit Calendar verb
        # when the request is primarily composition.
        if "calendar" in services and not re.search(
            r"\b(calendar|schedule|book|create|invite|reschedule|cancel)\b",
            authority_text,
        ):
            services.remove("calendar")
        if "gmail" in services and not GMAIL_DELIVERY_PATTERN.search(authority_text):
            services.remove("gmail")
        if "chat" in services and "chat" not in statement.delivery_channels:
            services.remove("chat")
        services.insert(0, "composition")
    write = (
        _matches(WRITE_PATTERNS, authority_text)
        or statement.current_authorizes_external_write
    ) and any(
        service != "composition" for service in services
    )
    high_risk = _matches(HIGH_RISK_PATTERNS, authority_text)
    if (
        any(service in services for service in ("gmail", "chat", "calendar"))
        and (
            re.search(
                r"\b(send|email|reply|post|invite|schedule|cancel)\b",
                authority_text,
            )
            or ("chat" in services and "chat" in statement.delivery_channels)
        )
    ):
        high_risk = True
    if not write:
        high_risk = False
    approval_bypassed = any(
        phrase in authority_text for phrase in APPROVAL_OPT_OUT
    )
    semantic = any(term in authority_text for term in SEMANTIC_TERMS)
    live = any(term in authority_text for term in LIVE_TERMS)
    rag_mode = "hybrid" if semantic and not live else "none"
    clarifications = []
    content_contract = analyze_content_request(authority_message or message)
    clarifications.extend(content_contract.required_clarifications)
    if "calendar" in services:
        if not re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", text):
            clarifications.append("What start time should the event use?")
        if not re.search(r"\b(?:\d+\s*(?:minutes?|hours?)|from\b.+\bto)\b", text):
            clarifications.append("How long should the event last?")
        if not re.search(r"\b(?:timezone|utc|gmt|ist|est|edt|pst|pdt|cet|asia/|america/|europe/)\b", text):
            clarifications.append("Which timezone should be used?")
        if re.search(r"\b(?:recurr(?:ing|ence)|repeat(?:ing|ed)?|every)\b", text):
            if not re.search(
                r"\b(?:daily|weekdays?|weekly|monthly|yearly|annually|"
                r"every\s+(?:day|week|month|year|monday|tuesday|wednesday|"
                r"thursday|friday|saturday|sunday))\b",
                text,
            ):
                clarifications.append(
                    "What recurrence should be used (daily, weekdays, weekly, monthly, or yearly)?"
                )
            if not re.search(r"\b(?:today|tomorrow|tommorow|on\s+\d{4}-\d{2}-\d{2})\b", text):
                clarifications.append("On what date should the recurrence start?")
            if not re.search(r"\b(?:until|through|for\s+\d+\s+(?:occurrences?|weeks?|months?))\b", text):
                clarifications.append("When should the recurrence end?")
    if (
        "chat" in services and write and "space" not in text
        and not statement.chat_destination_emails
    ):
        clarifications.append(
            "Which existing Google Chat space or direct-message email should "
            "receive the message?"
        )
    if (
        "gmail" in services
        and re.search(r"\b(?:count|how many)\b", text)
        and re.search(r"\btoday\b", text)
        and not resolved_timezone
    ):
        clarifications.append("Which timezone should define today?")
    if intent_kind == "ambiguous":
        clarifications.append(
            "What exact Workspace outcome should be performed for this service?"
        )
    if intent_kind not in {"workspace_action", "ambiguous"}:
        services = ["general"]
        write = False
        high_risk = False
        rag_mode = "none"
        clarifications = []
    return {
        "services": list(dict.fromkeys(services)),
        "write": write,
        "risk_level": "high" if high_risk else ("medium" if write else "low"),
        "requires_approval": high_risk and not approval_bypassed,
        "approval_bypassed": approval_bypassed,
        "rag_mode": rag_mode,
        "required_clarifications": clarifications,
        "intent_kind": intent_kind,
        "intent_evidence": intent_evidence,
        "informational_intent": intent_evidence.get("product_intent"),
        "sheet_url_is_drive_link": sheet_url_is_drive_link,
        "calendar_adds_meet": calendar_adds_meet,
        "timezone": resolved_timezone,
        "composition_requested": composition_requested,
        "authority_message": authority_message or message,
        "request_analysis": statement.classifier_input(),
        "content_contract": content_contract.plan_value(),
    }


def build_plan(
    message: str,
    timezone: str | None = None,
    *,
    authority_message: str | None = None,
    request_analysis: RequestStatementAnalysis | None = None,
    referenced_output: str | None = None,
) -> tuple[ExecutionPlan, dict]:
    statement = request_analysis or analyze_request_statement(
        authority_message or message
    )
    policy = classify_request(
        message, timezone, authority_message=authority_message,
        request_analysis=statement,
    )
    if (
        statement.contextual_reference
        and re.search(r"\blink\b", statement.normalized_text)
        and referenced_output is not None
        and not re.search(r"https?://[^\s<>()]+", referenced_output)
    ):
        policy["required_clarifications"] = list(dict.fromkeys([
            *policy["required_clarifications"],
            "Which exact link should be used? The referenced content contains no URL.",
        ]))
    # Transformation shorthand such as "make it shorter" is valid only when
    # relevance-gated conversation resolution supplied an actual source.  The
    # lexical composition classifier identifies the operation class, but it
    # must not manufacture missing content in a new session.
    contract_requested = bool(
        (policy.get("content_contract") or {}).get("requested")
    )
    resolved_transformation_source = bool(
        referenced_output
        or "Prior same-user, same-session context (reference only):" in message
    )
    if (
        policy["intent_kind"] == "workspace_action"
        and statement.composition_requested
        and not contract_requested
        and not resolved_transformation_source
        and not statement.explicit_services
    ):
        policy["intent_kind"] = "out_of_scope"
        policy["intent_evidence"] = {
            **(policy.get("intent_evidence") or {}),
            "basis": "composition transformation has no resolved source",
            "confidence": "high",
            "ambiguous": False,
        }
    if policy["intent_kind"] != "workspace_action":
        intent = policy["informational_intent"] or policy["intent_kind"]
        step = PlanStep(
            id="answer_workspace_conversation",
            title="Answer within the guarded Workspace conversation scope",
            service="general",
            operation=("answer_information" if policy["intent_kind"] == "product_information"
                       else "answer_workspace_chat"),
            arguments={
                "request": message,
                "informational_intent": intent,
                "intent_kind": policy["intent_kind"],
                "capability_catalog": capability_catalog(OPERATION_TOOLS),
                "okf_sources": approved_okf_capability_sources(),
                "allowed_tools": [],
            },
            read_only=True,
            risk_level="low",
            requires_approval=False,
            weight=1.0,
            preconditions=["The product capability registry is available"],
            postconditions=[
                "The answer is grounded in product identity and registered operations",
                "No Google API, user-content RAG, or language model call is made",
            ],
        )
        return ExecutionPlan(
            objective=message,
            intent_kind=policy["intent_kind"],
            required_clarifications=policy["required_clarifications"],
            services=["general"],
            rag_mode="none",
            steps=[step],
            success_criteria=step.postconditions,
            estimated_max_tokens=0,
        ), policy
    rag_answer_only = policy["rag_mode"] != "none" and not policy["write"]
    services = ["composition"] if rag_answer_only else (
        policy["services"] or ["general"]
    )
    ordered = sorted(services, key=lambda item: SERVICE_ORDER.get(item, 100))
    steps = []
    produced_data = []
    for service in ordered:
        operation_source = (
            message if statement.service_only else statement.normalized_text
        )
        operations = infer_operation_sequence(
            service,
            operation_source,
            policy["write"],
            allow_same_service_expansion=len(services) == 1,
        )
        # Exact Gmail copying is a typed read→write workflow even when natural
        # wording mentions "send" before "last mail" (for example, "send the
        # last mail you sent to A, send it to B"). Textual verb order must not
        # collapse that request into one search or one send step.
        if service == "gmail" and statement.gmail_copy_requested:
            operations = ["search", "send"]
        previous_same_service = None
        for operation_index, operation in enumerate(operations):
            step_id = (
                f"execute_{service}"
                if len(operations) == 1
                else f"execute_{service}_{operation}"
            )
            read_only = operation in READ_OPERATIONS
            postconditions = (
                [
                    "The answer uses only retrieved tenant evidence",
                    "Every claimed source retains its retrieval citation",
                    "No live Google read or external mutation is claimed",
                ]
                if rag_answer_only else SERVICE_POSTCONDITIONS.get(service, [
                    "The response contains deterministic evidence for every claimed result"
                ])
            )
            dependencies = []
            if previous_same_service:
                dependencies = [previous_same_service]
            elif service == "composition":
                dependencies = []
            elif "execute_composition" in produced_data:
                dependencies = ["execute_composition"]
            elif service == "sheets":
                dependencies = list(produced_data)
            elif service in {"chat", "calendar"} and "execute_sheets" in produced_data:
                dependencies = ["execute_sheets"]
            elif policy["write"] and service != "gmail" and produced_data:
                dependencies = [produced_data[-1]]
            count_match = re.search(
                r"\b(?:last|latest|recent)\s+(\d{1,3})\b", message.lower(),
            )
            exact_tool_arguments = {}
            if service == "gmail" and operation in {"message_count", "sender_count"}:
                category_match = re.search(
                    r"\b(promotions?|promotional|social|updates?|forums?|primary)\b",
                    message.casefold(),
                )
                category = {
                    "promotion": "promotions", "promotional": "promotions",
                    "update": "updates", "forum": "forums",
                }.get(
                    category_match.group(1), category_match.group(1),
                ) if category_match else None
                exact_tool_arguments = {
                    "category": category,
                    "period": (
                        "today"
                        if re.search(r"\btoday\b", message.casefold()) else "all"
                    ),
                    "timezone": policy["timezone"],
                    "max_messages": 500,
                }
            if service == "gmail" and operation == "recent_senders":
                category_match = re.search(
                    r"\b(promotions?|promotional|social|updates?|forums?|primary)\b",
                    message.casefold(),
                )
                category = {
                    "promotion": "promotions", "promotional": "promotions",
                    "update": "updates", "forum": "forums",
                }.get(category_match.group(1), category_match.group(1)) \
                    if category_match else None
                exact_tool_arguments = {
                    "max_results": (
                        min(int(count_match.group(1)), 100) if count_match else 20
                    ),
                    "query": "-in:sent",
                    "unique": not bool(re.search(
                        r"\b(?:keep|include) duplicates?\b", message.lower(),
                    )),
                    "category": category,
                    "period": (
                        "today" if re.search(r"\btoday\b", message.casefold())
                        else "all"
                    ),
                    "timezone": policy["timezone"],
                }
            gmail_copy = (
                service == "gmail"
                and operations == ["search", "send"]
                and len(statement.email_recipients) >= 2
                and (
                    statement.gmail_copy_requested
                    or bool(re.search(
                        r"\b(?:same|copy|forward)\s+(?:mail|email|message)\b",
                        statement.normalized_text,
                    ))
                )
            )
            if gmail_copy and operation == "search":
                exact_tool_arguments = {
                    "query": (
                        f"to:{statement.email_recipients[0]} in:sent"
                    ),
                    "max_results": 1,
                }
            if gmail_copy and operation == "send":
                exact_tool_arguments = {
                    "to": statement.email_recipients[-1],
                }
            if (
                service == "chat"
                and operation == "send"
                and (statement.chat_destination_emails or statement.email_recipients)
            ):
                exact_tool_arguments = {
                    "destination": (
                        statement.chat_destination_emails
                        or statement.email_recipients
                    )[0],
                }
                if statement.contextual_reference and referenced_output:
                    exact_tool_arguments["text"] = referenced_output
            if service == "calendar" and operation == "create":
                exact_tool_arguments = calendar_create_arguments(
                    message,
                    policy["timezone"],
                    statement.email_recipients,
                    add_meet=policy["calendar_adds_meet"],
                )
            if service == "composition":
                exact_tool_arguments = {
                    **exact_tool_arguments,
                    "content_contract": policy["content_contract"],
                }
                if statement.contextual_reference and referenced_output:
                    exact_tool_arguments["reuse_text"] = referenced_output
            allowed_tools = OPERATION_TOOLS.get((service, operation), [])
            contract = write_contract_for(service, operation, allowed_tools)
            steps.append(PlanStep(
                id=step_id,
                title=(
                    f"Execute and verify the {service} {operation} portion"
                    if len(operations) > 1
                    else f"Execute and verify the {service} portion"
                ),
                service=service,
                operation=operation,
                dependencies=dependencies,
                arguments={
                    "request": message,
                    "service": service,
                    "allowed_tools": allowed_tools,
                    "write_contract": (
                        {
                            "required_tools": list(contract.required_tools),
                            "completion_mode": contract.completion_mode,
                        }
                        if contract else None
                    ),
                    "tool_arguments": exact_tool_arguments,
                    "content_contract": (
                        policy["content_contract"]
                        if service == "composition" else None
                    ),
                    "workflow_hints": {
                        "answer_from_rag": rag_answer_only,
                        "extract_unique_sender_names": (
                            service == "gmail" and "people" in message.lower()
                        ),
                        "sheet_url_is_drive_link": policy["sheet_url_is_drive_link"],
                        "add_meet_conference": (
                            service == "calendar" and policy["calendar_adds_meet"]
                        ),
                        "copy_gmail_dependency": gmail_copy,
                    },
                    "semantic_authorization": (
                        {
                            "authorized": True,
                            "basis": "current_turn_semantic_historical_request",
                        }
                        if rag_answer_only else _service_authorization(
                            service, statement,
                        )
                    ),
                },
                read_only=read_only,
                risk_level=policy["risk_level"],
                requires_approval=policy["requires_approval"] and not read_only,
                weight=1.0,
                preconditions=(
                    ["Tenant-scoped retrieval completed"]
                    if rag_answer_only else ["Google authorization is valid"]
                ) + (
                    [f"Dependency {item} completed and its output is available"
                     for item in dependencies]
                ),
                postconditions=postconditions,
            ))
            previous_same_service = step_id
            if read_only or service in {"drive", "docs", "sheets"}:
                produced_data.append(step_id)
    success_criteria = [
        criterion for step in steps for criterion in step.postconditions
    ] + ["Partial results and the first failed step are reported accurately"]
    planned_services = {step.service for step in steps}
    # Coverage is checked against the classifier's canonical service frame,
    # not every lexical noun.  For example, "people who mailed me" is a
    # Gmail sender query rather than Contacts, and a semantic request for
    # historical documents is intentionally answered by the RAG composition
    # step rather than a live Docs operation.
    expected_services = {"composition"} if rag_answer_only else set(policy["services"])
    if policy["sheet_url_is_drive_link"]:
        expected_services.discard("drive")
    if policy["calendar_adds_meet"]:
        expected_services.discard("meet")
    missing_services = expected_services - planned_services
    if missing_services:
        raise ValueError(
            "Plan coverage rejected missing requested services: "
            + ", ".join(sorted(missing_services))
        )
    plan = ExecutionPlan(
        objective=message,
        intent_kind=policy["intent_kind"],
        required_clarifications=policy["required_clarifications"],
        services=ordered,
        rag_mode=policy["rag_mode"],
        steps=steps,
        success_criteria=success_criteria,
        estimated_max_tokens=min(
            10_000,
            sum(3_000 if step.service == "composition" else 1_500 for step in steps),
        ),
    )
    return plan, policy


def validate_plan(plan: ExecutionPlan) -> list[str]:
    errors = []
    keys = [step.id for step in plan.steps]
    if len(keys) != len(set(keys)):
        errors.append("Step identifiers must be unique")
    known = set(keys)
    registered = set(registered_tool_names())
    for step in plan.steps:
        missing = set(step.dependencies) - known
        if missing:
            errors.append(f"{step.id} has unknown dependencies: {sorted(missing)}")
        if step.id in step.dependencies:
            errors.append(f"{step.id} cannot depend on itself")
        if step.service not in SERVICE_ORDER:
            errors.append(f"{step.id} uses unknown service {step.service}")
        allowed = OPERATION_TOOLS.get((step.service, step.operation))
        if step.service != "general" and allowed is None:
            errors.append(f"{step.id} uses unknown operation {step.operation}")
            continue
        allowed = list(step.arguments.get("allowed_tools") or allowed or [])
        projected = step.arguments.get("write_contract")
        required = list((projected or {}).get("required_tools") or [])
        if step.read_only and any(tool in WRITE_TOOLS for tool in required):
            errors.append(f"{step.id} is read-only but requires a write tool")
        if not step.read_only:
            semantic = step.arguments.get("semantic_authorization") or {}
            if semantic.get("authorized") is False:
                errors.append(
                    f"{step.id} lacks current-turn semantic authorization"
                )
            contract = write_contract_for(step.service, step.operation, allowed)
            if not contract or not required:
                errors.append(f"{step.id} has no valid required write contract")
            elif (
                required != list(contract.required_tools)
                or (projected or {}).get("completion_mode") != contract.completion_mode
            ):
                errors.append(f"{step.id} has a mismatched write contract")
        if not set(required).issubset(allowed):
            errors.append(f"{step.id} requires tools outside its allowed tool ceiling")
        unknown_required = set(required) - registered
        if unknown_required:
            errors.append(
                f"{step.id} requires unregistered tools: {sorted(unknown_required)}"
            )
    positions = {key: index for index, key in enumerate(keys)}
    for step in plan.steps:
        if any(positions[dependency] >= positions[step.id] for dependency in step.dependencies):
            errors.append(f"{step.id} has a forward/cyclic dependency")
    return errors


def action_hash(plan: ExecutionPlan) -> str:
    canonical = json.dumps(plan.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
