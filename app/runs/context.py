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
                """SELECT id,request,result,intent_kind,status
                   FROM agent_runs
                   WHERE user_id=$1 AND session_id=$2 AND deleted_at IS NULL
                     AND queued_at >= now()-interval '24 hours'
                   ORDER BY queued_at DESC LIMIT 1""",
                user_id, session_id,
            )
        else:
            # Referential delivery must resolve to content, not blindly to the
            # last assistant turn. Skip capability/scope answers and write
            # receipts, and prefer a composition when the user explicitly names
            # a paragraph/draft/essay even if an intervening read occurred.
            wants_composition = bool(
                re.search(
                    r"\b(paragraph|draft|essay|roadmap|application|letter)\b",
                    statement.normalized_text,
                )
            )
            previous = await conn.fetchrow(
                """SELECT id,request,result,intent_kind,status
                   FROM agent_runs
                   WHERE user_id=$1 AND session_id=$2 AND deleted_at IS NULL
                     AND queued_at >= now()-interval '24 hours'
                     AND status='completed'
                     AND nullif(btrim(result->>'output'),'') IS NOT NULL
                     AND intent_kind NOT IN
                       ('product_information','scope_chat','workspace_guidance',
                        'ambiguous','out_of_scope')
                     AND NOT EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements(
                         coalesce(plan->'steps','[]'::jsonb)
                       ) AS planned_step
                       WHERE coalesce(
                         (planned_step->>'read_only')::boolean, true
                       ) = false
                     )
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

    prior_output = ""
    result = previous["result"]
    if isinstance(result, dict):
        prior_output = _bounded(result.get("output"), 6_000)
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
    )
