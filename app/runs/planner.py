import hashlib
import json
import re

from app.runs.schemas import ExecutionPlan, PlanStep
from app.runs.informational import (
    capability_catalog,
    classify_workspace_intent,
    approved_okf_capability_sources,
)

SERVICES = {
    "gmail": ("gmail", "email", "emails", "mail", "mails"),
    "calendar": ("calendar", "event", "schedule", "invite", "meeting"),
    "drive": ("drive", "file", "files", "folder", "folders", "share"),
    "docs": ("doc", "docs", "document", "documents"),
    "sheets": ("sheet", "spreadsheet", "table", "rows"),
    "tasks": ("task", "tasks", "todo"),
    "chat": ("chat", "space"),
    "contacts": ("contact", "contacts", "people"),
    "meet": ("meet", "conference", "video call"),
}

WRITE_PATTERNS = (
    r"\bsend\b", r"\breply\b", r"\bcreate\b", r"\bwrite\b", r"\bappend\b",
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
    "contacts": 10, "gmail": 20, "drive": 30, "docs": 40, "sheets": 50,
    "tasks": 60, "chat": 70, "calendar": 80, "meet": 90, "general": 100,
}

SERVICE_POSTCONDITIONS = {
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
    "gmail": [("trash", r"\b(trash|delete)\b"), ("label", r"\blabel\b"),
              ("reply", r"\brepl(?:y|ies)\b"), ("send", r"\bsend\b"),
              ("search", r"\b(search|find|latest|recent|last|get|read|list)\b")],
    "calendar": [("delete", r"\b(delete|cancel)\b"), ("update", r"\b(update|move|reschedule)\b"),
                 ("create", r"\b(create|schedule|invite|book)\b"),
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
    ("chat", "send"): ["list_chat_spaces", "send_chat_message"],
    ("chat", "list_spaces"): ["list_chat_spaces"],
    ("contacts", "search"): ["search_contacts", "get_contact"],
    ("contacts", "get"): ["get_contact"],
    ("meet", "create"): ["create_meet_space", "get_meet_space"],
    ("meet", "participants"): ["list_meet_participants"],
    ("meet", "conferences"): ["list_meet_conferences", "get_meet_space"],
    ("meet", "get"): ["get_meet_space"],
}
READ_OPERATIONS = {"search", "get", "read", "list", "availability", "list_spaces",
                   "participants", "conferences"}
DEFAULT_READ_OPERATION = {
    "gmail": "search", "calendar": "list", "drive": "search", "docs": "read",
    "sheets": "read", "tasks": "list", "chat": "list_spaces", "contacts": "search",
    "meet": "conferences",
}


def infer_operation(service: str, message: str, write: bool) -> str:
    text = message.lower()
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


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def classify_request(message: str) -> dict:
    text = " ".join(message.lower().split())
    services = [
        service for service, terms in SERVICES.items()
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)
    ]
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
    intent_kind, intent_evidence = classify_workspace_intent(message, services)
    write = _matches(WRITE_PATTERNS, text)
    high_risk = _matches(HIGH_RISK_PATTERNS, text)
    approval_bypassed = any(phrase in text for phrase in APPROVAL_OPT_OUT)
    semantic = any(term in text for term in SEMANTIC_TERMS)
    live = any(term in text for term in LIVE_TERMS)
    rag_mode = "hybrid" if semantic and not live else "none"
    clarifications = []
    if any(term in text for term in ("schedule", "calendar", "meeting", "invite")):
        if not re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", text):
            clarifications.append("What start time should the event use?")
        if not re.search(r"\b(?:\d+\s*(?:minutes?|hours?)|from\b.+\bto)\b", text):
            clarifications.append("How long should the event last?")
        if not re.search(r"\b(?:timezone|utc|gmt|ist|est|edt|pst|pdt|cet|asia/|america/|europe/)\b", text):
            clarifications.append("Which timezone should be used?")
    if "chat" in services and write and "space" not in text:
        clarifications.append("Which Google Chat space should receive the message?")
    if intent_kind != "workspace_action":
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
    }


def build_plan(message: str) -> tuple[ExecutionPlan, dict]:
    policy = classify_request(message)
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
            services=["general"],
            rag_mode="none",
            steps=[step],
            success_criteria=step.postconditions,
            estimated_max_tokens=0,
        ), policy
    services = policy["services"] or ["general"]
    ordered = sorted(services, key=lambda item: SERVICE_ORDER.get(item, 100))
    steps = []
    produced_data = []
    for service in ordered:
        step_id = f"execute_{service}"
        operation = infer_operation(service, message, policy["write"])
        read_only = operation in READ_OPERATIONS
        postconditions = SERVICE_POSTCONDITIONS.get(service, [
            "The response contains deterministic evidence for every claimed result"
        ])
        dependencies = []
        if service == "sheets":
            dependencies = list(produced_data)
        elif service in {"chat", "calendar"} and "execute_sheets" in produced_data:
            dependencies = ["execute_sheets"]
        elif policy["write"] and service != "gmail" and produced_data:
            dependencies = [produced_data[-1]]
        steps.append(PlanStep(
            id=step_id,
            title=f"Execute and verify the {service} portion",
            service=service,
            operation=operation,
            dependencies=dependencies,
            arguments={"request": message, "service": service,
                       "allowed_tools": OPERATION_TOOLS.get((service, operation), []),
                       "workflow_hints": {
                           "extract_unique_sender_names": service == "gmail" and "people" in message.lower(),
                           "sheet_url_is_drive_link": policy["sheet_url_is_drive_link"],
                           "add_meet_conference": service == "calendar" and policy["calendar_adds_meet"],
                       }},
            read_only=read_only,
            risk_level=policy["risk_level"],
            requires_approval=policy["requires_approval"],
            weight=1.0,
            preconditions=["Google authorization is valid"] + (
                [f"Dependency {item} completed and its output is available"
                 for item in dependencies]
            ),
            postconditions=postconditions,
        ))
        if read_only or service in {"drive", "docs", "sheets"}:
            produced_data.append(step_id)
    success_criteria = [
        criterion for step in steps for criterion in step.postconditions
    ] + ["Partial results and the first failed step are reported accurately"]
    plan = ExecutionPlan(
        objective=message,
        intent_kind=policy["intent_kind"],
        required_clarifications=policy["required_clarifications"],
        services=ordered,
        rag_mode=policy["rag_mode"],
        steps=steps,
        success_criteria=success_criteria,
        estimated_max_tokens=min(8_000, 1_500 * len(steps)),
    )
    return plan, policy


def validate_plan(plan: ExecutionPlan) -> list[str]:
    errors = []
    keys = [step.id for step in plan.steps]
    if len(keys) != len(set(keys)):
        errors.append("Step identifiers must be unique")
    known = set(keys)
    for step in plan.steps:
        missing = set(step.dependencies) - known
        if missing:
            errors.append(f"{step.id} has unknown dependencies: {sorted(missing)}")
        if step.id in step.dependencies:
            errors.append(f"{step.id} cannot depend on itself")
        if step.service not in SERVICE_ORDER:
            errors.append(f"{step.id} uses unknown service {step.service}")
        allowed = OPERATION_TOOLS.get((step.service, step.operation))
        if step.service != "general" and not allowed:
            errors.append(f"{step.id} uses unknown operation {step.operation}")
    positions = {key: index for index, key in enumerate(keys)}
    for step in plan.steps:
        if any(positions[dependency] >= positions[step.id] for dependency in step.dependencies):
            errors.append(f"{step.id} has a forward/cyclic dependency")
    return errors


def action_hash(plan: ExecutionPlan) -> str:
    canonical = json.dumps(plan.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
