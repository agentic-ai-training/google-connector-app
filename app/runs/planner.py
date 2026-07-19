import hashlib
import json
import re

from app.runs.schemas import ExecutionPlan, PlanStep

SERVICES = {
    "gmail": ("gmail", "email", "mail"),
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


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def classify_request(message: str) -> dict:
    text = " ".join(message.lower().split())
    services = [
        service for service, terms in SERVICES.items()
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)
    ]
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
    return {
        "services": list(dict.fromkeys(services)),
        "write": write,
        "risk_level": "high" if high_risk else ("medium" if write else "low"),
        "requires_approval": high_risk and not approval_bypassed,
        "approval_bypassed": approval_bypassed,
        "rag_mode": rag_mode,
        "required_clarifications": clarifications,
    }


def build_plan(message: str) -> tuple[ExecutionPlan, dict]:
    policy = classify_request(message)
    services = policy["services"] or ["general"]
    ordered = sorted(services, key=lambda item: SERVICE_ORDER.get(item, 100))
    steps = []
    previous = None
    for service in ordered:
        step_id = f"execute_{service}"
        postconditions = SERVICE_POSTCONDITIONS.get(service, [
            "The response contains deterministic evidence for every claimed result"
        ])
        steps.append(PlanStep(
            id=step_id,
            title=f"Execute and verify the {service} portion",
            service=service,
            operation="read" if not policy["write"] else "execute_and_verify",
            dependencies=[previous] if previous else [],
            arguments={"request": message, "service": service},
            read_only=not policy["write"],
            risk_level=policy["risk_level"],
            requires_approval=policy["requires_approval"],
            weight=1.0,
            preconditions=["Google authorization is valid"] + (
                [f"Dependency {previous} completed and its output is available"]
                if previous else []
            ),
            postconditions=postconditions,
        ))
        previous = step_id
    success_criteria = [
        criterion for step in steps for criterion in step.postconditions
    ] + ["Partial results and the first failed step are reported accurately"]
    plan = ExecutionPlan(
        objective=message,
        required_clarifications=policy["required_clarifications"],
        services=services,
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
    positions = {key: index for index, key in enumerate(keys)}
    for step in plan.steps:
        if any(positions[dependency] >= positions[step.id] for dependency in step.dependencies):
            errors.append(f"{step.id} has a forward/cyclic dependency")
    return errors


def action_hash(plan: ExecutionPlan) -> str:
    canonical = json.dumps(plan.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
