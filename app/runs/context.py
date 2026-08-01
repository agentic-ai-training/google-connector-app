"""Bounded, tenant-scoped conversation context for durable run planning."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.runs.request_analysis import (
    RequestStatementAnalysis,
    analyze_request_statement,
)


@dataclass(frozen=True)
class ContextAnalysis:
    current_message: str
    effective_message: str
    mode: str = "standalone"
    source_run_ids: list[str] = field(default_factory=list)
    context_turn_count: int = 0
    relevance_reason: str = "self_contained"
    current_authorizes_external_write: bool = False
    # This content is deliberately omitted from diagnostics. It is passed only
    # to the typed planner so a referential delivery can bind the exact prior
    # assistant result without granting that result service authority.
    referenced_output: str | None = None
    referenced_subject: str | None = None
    referenced_service: str | None = None
    referenced_kind: str | None = None

    def diagnostics(self) -> dict:
        return {
            "version": "conversation-context-v1",
            "mode": self.mode,
            "analyzed_for_every_request": True,
            "prior_context_included": bool(self.source_run_ids),
            "relevance_reason": self.relevance_reason,
            "source_run_ids": self.source_run_ids,
            "context_turn_count": self.context_turn_count,
            "current_authorizes_external_write": (
                self.current_authorizes_external_write
            ),
            "current_message_characters": len(self.current_message),
            "effective_message_characters": len(self.effective_message),
        }


def _relevance_reason(analysis: RequestStatementAnalysis) -> str | None:
    if analysis.service_only:
        return "service_only_clarification"
    if analysis.contextual_reference:
        return "referential_action"
    return None


def _bounded(value: str | None, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _referenced_content(previous) -> tuple[str, str | None, str | None, str | None]:
    """Extract exact reusable content, never an assistant receipt or placeholder."""
    step_outputs = previous.get("step_outputs") or []
    if isinstance(step_outputs, str):
        import json
        try:
            step_outputs = json.loads(step_outputs)
        except ValueError:
            step_outputs = []
    for output in reversed(step_outputs):
        if not isinstance(output, dict):
            continue
        for execution in reversed(output.get("tool_executions") or []):
            if not isinstance(execution, dict):
                continue
            arguments = execution.get("arguments") or {}
            tool = execution.get("tool")
            if tool == "send_gmail" and arguments.get("body"):
                return (
                    _bounded(arguments["body"], 6_000),
                    _bounded(arguments.get("subject"), 500) or None,
                    "gmail", "verified_sent_message",
                )
            if tool == "send_chat_message" and arguments.get("text"):
                return (
                    _bounded(arguments["text"], 6_000), None,
                    "chat", "verified_sent_message",
                )
            if tool in {"create_calendar_event", "update_calendar_event"}:
                result = execution.get("result") or {}
                if not isinstance(result, dict):
                    continue
                title = _bounded(result.get("summary") or arguments.get("title"), 500)
                start = _bounded((result.get("start") or {}).get("dateTime"), 200)
                end = _bounded((result.get("end") or {}).get("dateTime"), 200)
                recurrence = ", ".join(
                    _bounded(value, 500) for value in (result.get("recurrence") or [])
                )
                url = _bounded(result.get("htmlLink"), 2_000)
                fields = [
                    f"Calendar event: {title}" if title else "Calendar event",
                    f"Starts: {start}" if start else "",
                    f"Ends: {end}" if end else "",
                    f"Recurrence: {recurrence}" if recurrence else "",
                    f"Link: {url}" if url else "",
                ]
                content = "\n".join(field for field in fields if field)
                if result.get("id") and content:
                    return content, None, "calendar", "verified_calendar_event"
        if output.get("content_lineage") and output.get("output"):
            return (
                _bounded(output["output"], 6_000), None,
                "composition", "verified_composition",
            )
    result = previous.get("result")
    if isinstance(result, str):
        import json
        try:
            result = json.loads(result)
        except ValueError:
            result = {}
    if isinstance(result, dict):
        output = _bounded(result.get("output"), 6_000)
        if output:
            return output, None, None, "assistant_output"
    return "", None, None, None


async def analyze_conversation_context(
    pool,
    *,
    user_id: str,
    session_id: str,
    message: str,
    request_analysis: RequestStatementAnalysis | None = None,
) -> ContextAnalysis:
    """Resolve only explicit contextual language against recent same-session runs."""
    statement = request_analysis or analyze_request_statement(message)
    external_write = statement.current_authorizes_external_write
    relevance_reason = _relevance_reason(statement)
    if not relevance_reason:
        return ContextAnalysis(
            current_message=message,
            effective_message=message,
            relevance_reason="self_contained",
            current_authorizes_external_write=external_write,
        )

    service = statement.service_only
    async with pool.acquire() as conn:
        if service:
            # A service-only answer ("gmail") applies only to the immediately
            # preceding ambiguous turn. It must never skip backwards and attach
            # itself to an unrelated request.
            previous = await conn.fetchrow(
                """SELECT r.id,r.request,r.result,r.intent_kind,r.status,
                          (SELECT coalesce(jsonb_agg(s.output_data ORDER BY s.sequence_no),
                                           '[]'::jsonb)
                             FROM agent_run_steps s WHERE s.run_id=r.id
                               AND s.status='completed') AS step_outputs
                   FROM agent_runs
                   AS r
                   WHERE user_id=$1 AND session_id=$2 AND deleted_at IS NULL
                     AND queued_at >= now()-interval '24 hours'
                   ORDER BY queued_at DESC LIMIT 1""",
                user_id, session_id,
            )
        else:
            # Referential delivery resolves exact content from completed step
            # evidence. Verified writes are eligible because their tool arguments
            # contain the message that was actually sent; their prose receipts are
            # never reused as content.
            wants_composition = bool(
                re.search(
                    r"\b(paragraph|draft|essay|roadmap|application|letter)\b",
                    statement.normalized_text,
                )
            )
            previous = await conn.fetchrow(
                """SELECT r.id,r.request,r.result,r.intent_kind,r.status,
                          (SELECT coalesce(jsonb_agg(s.output_data ORDER BY s.sequence_no),
                                           '[]'::jsonb)
                             FROM agent_run_steps s WHERE s.run_id=r.id
                               AND s.status='completed') AS step_outputs
                   FROM agent_runs AS r
                   WHERE user_id=$1 AND session_id=$2 AND deleted_at IS NULL
                     AND queued_at >= now()-interval '24 hours'
                     AND status='completed'
                     AND nullif(btrim(result->>'output'),'') IS NOT NULL
                     AND intent_kind NOT IN
                       ('product_information','scope_chat','workspace_guidance',
                        'ambiguous','out_of_scope')
                   ORDER BY
                     CASE WHEN $3 AND coalesce(plan->'services','[]'::jsonb)
                                      ? 'composition'
                          THEN 0 ELSE 1 END,
                     queued_at DESC
                   LIMIT 1""",
                user_id, session_id, wants_composition,
            )
    if not previous:
        return ContextAnalysis(
            current_message=message,
            effective_message=message,
            relevance_reason="no_recent_same_session_context",
            current_authorizes_external_write=external_write,
        )
    if service and previous["intent_kind"] not in {"ambiguous", "out_of_scope"}:
        return ContextAnalysis(
            current_message=message,
            effective_message=message,
            relevance_reason="service_clarification_not_applicable",
            current_authorizes_external_write=external_write,
        )

    prior_output, prior_subject, prior_service, prior_kind = _referenced_content(
        previous
    )
    prior_request = _bounded(previous["request"], 4_000)
    context_lines = [
        "Current request (the only authority for new external actions):",
        message,
        "",
        "Prior same-user, same-session context (reference only):",
        f"Previous user request: {prior_request}",
    ]
    if prior_output:
        context_lines.append(f"Previous assistant result: {prior_output}")
    if service:
        context_lines.append(f"Current service clarification: {service}")
    return ContextAnalysis(
        current_message=message,
        effective_message="\n".join(context_lines),
        mode="service_clarification" if service else "contextual_reference",
        source_run_ids=[str(previous["id"])],
        context_turn_count=1,
        relevance_reason=relevance_reason,
        current_authorizes_external_write=external_write,
        referenced_output=prior_output or None,
        referenced_subject=prior_subject,
        referenced_service=prior_service,
        referenced_kind=prior_kind,
    )
