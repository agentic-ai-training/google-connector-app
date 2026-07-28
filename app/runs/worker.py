import asyncio
import json
import logging
import socket
import time
import uuid
from contextlib import suppress

from app.config.settings import get_settings
from app.agents.errors import ExecutionFailure
from app.agents.supervisor import (
    execute_tool_call, get_toolsets, safe_write_failure_message,
)
from app.db.google_clients import request_google_credentials
from app.db.oauth_credentials import load_google_credentials
from app.runs.incident import build_incident, completion_from_steps
from app.runs.repository import append_event
from app.runs.informational import informational_answer, workspace_chat_answer
from app.improvements.failure_intelligence import record_failure_incident
from app.runs.verifier import verify_executions_detailed
from app.mlops.metrics import (
    postcondition_failures, run_duration, run_failures, run_transitions,
)
from app.evaluation.collector import record_run_evaluation
from app.tools.base import GoogleWorkspaceBaseTool
from app.tools.result_projection import project_tool_result
from app.tools.contracts import write_contract_for

logger = logging.getLogger(__name__)


def classify_error(exc: Exception) -> str:
    if isinstance(exc, ExecutionFailure):
        return exc.category
    text = str(exc).lower()
    if "429" in text or "rate limit" in text or "quota" in text:
        return "rate_limit"
    if "oauth" in text or "credential" in text or "unauthorized" in text:
        return "authentication"
    if "permission" in text or "403" in text:
        return "permission"
    if any(code in text for code in ("500", "502", "503", "504")):
        return "network"
    if "timeout" in text or "connection" in text or "temporarily unavailable" in text:
        return "network"
    return "execution"


async def claim_run(pool, owner: str):
    settings = get_settings()
    lease = settings.worker_lease_seconds
    executor_version = settings.executor_version or settings.deployment_version
    executor_role = settings.executor_role
    if executor_role not in {"control", "candidate"}:
        raise RuntimeError("EXECUTOR_ROLE must be control or candidate")
    while True:
        terminal_recovery = None
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """SELECT * FROM agent_runs
                   WHERE (status='queued' OR
                         (status='running' AND lease_expires_at < now()))
                     AND (($2='candidate' AND executor_version=$1
                           AND cohort_assignment='candidate')
                          OR ($2='control' AND executor_version=$1))
                     AND deleted_at IS NULL
                   ORDER BY queued_at
                   FOR UPDATE SKIP LOCKED LIMIT 1""",
                executor_version, executor_role,
            )
            if not row:
                return None
            if row["status"] == "running":
                interrupted = await conn.fetch(
                    """SELECT * FROM agent_run_steps
                       WHERE run_id=$1 AND status='running' ORDER BY sequence_no
                       FOR UPDATE""",
                    row["id"],
                )
                retryable = interrupted and all(
                    step["read_only"] and step["attempt_count"] < step["max_attempts"]
                    for step in interrupted
                )
                if retryable:
                    await conn.execute(
                        """UPDATE agent_run_steps SET status='pending',started_at=NULL,
                           completed_at=NULL,error_category='worker',
                           error_message='Worker lease expired; safe read scheduled again'
                           WHERE run_id=$1 AND status='running'""",
                        row["id"],
                    )
                    await conn.execute(
                        """INSERT INTO agent_run_events
                           (run_id,user_id,event_type,phase,message,payload)
                           VALUES($1,$2,'lease_recovered','recovery',
                                  'Expired worker lease recovered; interrupted reads requeued',
                                  $3::jsonb)""",
                        row["id"], row["user_id"],
                        json.dumps({"step_count": len(interrupted), "write_retried": False}),
                    )
                elif interrupted:
                    reconciliation_required = any(
                        not step["read_only"] for step in interrupted
                    )
                    category = (
                        "worker_reconciliation" if reconciliation_required else "worker"
                    )
                    error = (
                        "Worker lease expired while an external write may have completed; "
                        "automatic retry is blocked pending reconciliation"
                        if reconciliation_required else
                        "Worker lease expired and the safe read retry budget was exhausted"
                    )
                    await conn.execute(
                        """UPDATE agent_run_steps SET status='failed',completed_at=now(),
                           error_category=$1,error_message=$2
                           WHERE run_id=$3 AND status='running'""",
                        category, error, row["id"],
                    )
                    steps = [dict(step) for step in await conn.fetch(
                        "SELECT * FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no",
                        row["id"],
                    )]
                    completion = completion_from_steps(steps)
                    if reconciliation_required:
                        completion["side_effect_integrity"] = 0
                    incident = build_incident(steps, category, error)
                    status = (
                        "partial" if any(step["status"] == "completed" for step in steps)
                        else "failed"
                    )
                    updated = await conn.fetchrow(
                        """UPDATE agent_runs SET status=$1,current_phase=$2,
                           incident_summary=$3::jsonb,technical_completion=$4,
                           functional_completion=$5,user_visible_completion=$6,
                           side_effect_integrity=$7,error_category=$8,
                           error_message=$9,completed_at=now(),current_step_id=NULL,
                           lease_owner=NULL,lease_expires_at=NULL WHERE id=$10 RETURNING *""",
                        status, "reconciliation" if reconciliation_required else status,
                        json.dumps(incident), completion["technical_completion"],
                        completion["functional_completion"],
                        completion["user_visible_completion"],
                        completion["side_effect_integrity"], category, error, row["id"],
                    )
                    await conn.execute(
                        """INSERT INTO agent_run_events
                           (run_id,user_id,event_type,phase,message,payload)
                           VALUES($1,$2,$3,'recovery',$4,
                                  $5::jsonb)""",
                        row["id"], row["user_id"],
                        ("write_reconciliation_required" if reconciliation_required
                         else "lease_retry_exhausted"), error,
                        json.dumps({
                            "automatic_retry": False,
                            "interrupted_steps": [step["step_key"] for step in interrupted],
                        }),
                    )
                    terminal_recovery = {
                        "run": dict(updated), "steps": steps, "completion": completion,
                        "incident": incident, "error": error, "category": category,
                        "reconciliation_required": reconciliation_required,
                        "failed_step": next(
                            (step for step in steps if step["status"] == "failed"), None
                        ),
                    }
            if terminal_recovery is not None:
                updated = None
            else:
                updated = await conn.fetchrow(
                    """UPDATE agent_runs SET status='running',current_phase='execution',
                       started_at=COALESCE(started_at,now()),heartbeat_at=now(),
                       executor_version=CASE WHEN canary_id IS NULL THEN $4
                                             ELSE executor_version END,
                       lease_owner=$1,lease_expires_at=now()+($2 * interval '1 second')
                       WHERE id=$3 RETURNING *""",
                    owner, lease, row["id"], executor_version,
                )
        if terminal_recovery is None:
            return dict(updated)
        failed_step = terminal_recovery["failed_step"]
        run = terminal_recovery["run"]
        try:
            failure_record = await record_failure_incident(
                pool,
                occurrence_key=(
                    f"run:{run['id']}:stale-write" if
                    terminal_recovery["reconciliation_required"] else
                    f"run:{run['id']}:stale-read-exhausted"
                ),
                run_id=run["id"],
                session_id=run["session_id"], user_id=run["user_id"],
                message=run["request"],
                intent_kind=run.get("intent_kind") or "workspace_action",
                stage="recovery", category=terminal_recovery["category"],
                component="durable_worker", error=terminal_recovery["error"],
                service=failed_step["service"] if failed_step else None,
                operation=failed_step["operation"] if failed_step else None,
                breaking_point=(failed_step["title"] if failed_step else None),
                completion=terminal_recovery["completion"],
                evidence={
                    "automatic_retry": False,
                    "reason": ("expired_write_lease" if
                               terminal_recovery["reconciliation_required"] else
                               "read_retry_budget_exhausted"),
                },
                policy=run.get("plan") or {},
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_runs SET failure_fingerprint=$1 WHERE id=$2",
                    failure_record["failure_fingerprint"], run["id"],
                )
        except Exception:
            logger.exception("Unable to persist stale-write incident for run %s", run["id"])
        await record_run_evaluation(pool, run["id"])
        run_transitions.labels(run["status"]).inc()
        run_failures.labels(terminal_recovery["category"]).inc()
        return {**run, "_terminal_recovery": True}


async def _heartbeat(pool, run_id, owner):
    while True:
        await asyncio.sleep(max(5, get_settings().worker_lease_seconds // 3))
        async with pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE agent_runs SET heartbeat_at=now(),
                   lease_expires_at=now()+($1 * interval '1 second')
                   WHERE id=$2 AND lease_owner=$3 AND status='running'""",
                get_settings().worker_lease_seconds, run_id, owner,
            )
        if result.endswith("0"):
            return


def _contains_failure(value) -> bool:
    if isinstance(value, dict):
        if value.get("error") or value.get("success") is False:
            return True
        return any(_contains_failure(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_failure(item) for item in value)
    return False


def _find_artifacts(value, found=None):
    """Extract stable Google resource evidence without retaining message bodies."""
    found = found if found is not None else []
    if isinstance(value, dict):
        external_id = next((str(value[key]) for key in (
            "spreadsheetId", "documentId", "fileId", "messageId", "eventId",
            "taskId", "spaceId", "conferenceId", "id", "name",
        ) if value.get(key)), None)
        url = next((str(value[key]) for key in (
            "spreadsheetUrl", "documentUrl", "webViewLink", "htmlLink", "meetLink",
            "meetingUri", "url", "link",
        ) if value.get(key)), None)
        if external_id or url:
            found.append({"external_id": external_id, "url": url})
        for item in value.values():
            _find_artifacts(item, found)
    elif isinstance(value, list):
        for item in value:
            _find_artifacts(item, found)
    return found


def verify_step(step, result) -> tuple[bool, str, list[dict]]:
    """Legacy structural verifier retained for old graph/test compatibility."""
    tool_results = result.get("tool_results", [])
    if _contains_failure(tool_results):
        return False, "At least one tool returned explicit failure evidence", []
    artifacts = _find_artifacts(tool_results)
    if not step["read_only"] and not tool_results:
        return False, "A write step completed without any tool result", []
    if not step["read_only"] and not artifacts:
        return False, "A write step returned no stable resource ID or URL", []
    if not result.get("task_complete"):
        return False, result.get("error") or "The agent did not reach a completed state", artifacts
    return True, "Deterministic postconditions passed", artifacts


async def _claim_step(conn, run_id):
    return await conn.fetchrow(
        """UPDATE agent_run_steps SET status='running',attempt_count=attempt_count+1,
           started_at=now() WHERE id=(
             SELECT candidate.id FROM agent_run_steps candidate
             WHERE candidate.run_id=$1 AND candidate.status='pending'
               AND NOT EXISTS (
                 SELECT 1 FROM unnest(candidate.dependencies) dependency
                 LEFT JOIN agent_run_steps required
                   ON required.run_id=candidate.run_id AND required.step_key=dependency
                 WHERE required.id IS NULL OR required.status!='completed'
               )
             ORDER BY candidate.sequence_no FOR UPDATE SKIP LOCKED LIMIT 1
           ) RETURNING *""",
        run_id,
    )


async def _dependency_context(conn, step):
    if not step["dependencies"]:
        return []
    rows = await conn.fetch(
        """SELECT step_key,service,output_data FROM agent_run_steps
           WHERE run_id=$1 AND step_key=ANY($2::text[]) ORDER BY sequence_no""",
        step["run_id"], step["dependencies"],
    )
    projected = []
    for row in rows:
        value = dict(row)
        envelope = project_tool_result(
            "dependency_output", value.get("output_data") or {},
            max_tokens=get_settings().groq_tool_result_max_tokens,
        )
        value["output_data"] = envelope.compact_result
        value["projection"] = envelope.metadata()
        projected.append(value)
    return projected


def _persistable_executions(executions: list[dict]) -> list[dict]:
    """Remove raw payloads while retaining compact evidence and lineage."""
    return [{
        "tool": item.get("tool"),
        "arguments": item.get("arguments") or {},
        "result": item.get("compact_result", item.get("result")),
        "projection": item.get("projection") or {},
    } for item in executions]


def _recent_sender_rows(dependencies: list[dict]) -> list[list[str]] | None:
    """Project a recent-sender dependency into deterministic Sheet rows."""
    for dependency in dependencies:
        output = dependency.get("output_data") or {}
        if not isinstance(output, dict):
            continue
        for execution in output.get("tool_executions") or []:
            if execution.get("tool") != "list_recent_gmail_senders":
                continue
            result = execution.get("result") or {}
            senders = result.get("senders") if isinstance(result, dict) else None
            if not isinstance(senders, list):
                continue
            rows = [["Name", "Email"]]
            rows.extend([
                [
                    str(item.get("sender_name") or item.get("sender_email") or ""),
                    str(item.get("sender_email") or ""),
                ]
                for item in senders if isinstance(item, dict)
            ])
            return rows
    return None


async def _create_recent_senders_sheet(pool, run, step, dependencies):
    """Create and populate a Sheet without asking a model to copy resource IDs."""
    rows = _recent_sender_rows(dependencies)
    if rows is None:
        return None
    tools = {tool.name: tool for tool in get_toolsets()["sheets"]}
    for tool in tools.values():
        if isinstance(tool, GoogleWorkspaceBaseTool):
            tool.db_pool = pool
    state = {
        "session_id": run["session_id"], "user_id": run["user_id"],
        "run_id": str(run["id"]), "step_id": str(step["id"]),
    }
    title = f"Recent Gmail Senders - {str(run['id'])[:8]}"
    create_call = {
        "id": f"deterministic-create-{uuid.uuid4()}",
        "name": "create_google_sheet", "args": {"title": title},
    }
    _, create_result, create_envelope = await execute_tool_call(
        tools["create_google_sheet"], create_call, state, pool,
    )
    executions = [{
        "tool": create_call["name"], "arguments": create_call["args"],
        "result": create_result, "compact_result": create_envelope.compact_result,
        "projection": create_envelope.metadata(),
    }]
    if not isinstance(create_result, dict) or create_result.get("error"):
        return {
            "output": "", "tool_results": [create_envelope.compact_result],
            "tool_executions": executions, "task_complete": False,
            "error": (
                f"{safe_write_failure_message('create_google_sheet', create_result)}; "
                "automatic repetition is blocked."
            ),
            "error_category": "tool_failure",
            "error_component": "deterministic_sheet_workflow",
            "error_boundary": "sheet_create",
        }
    spreadsheet_id = create_result.get("spreadsheetId")
    write_call = {
        "id": f"deterministic-write-{uuid.uuid4()}",
        "name": "write_google_sheet",
        "args": {
            "spreadsheet_id": spreadsheet_id,
            "range": f"Sheet1!A1:B{len(rows)}",
            "values": rows,
        },
    }
    _, write_result, write_envelope = await execute_tool_call(
        tools["write_google_sheet"], write_call, state, pool,
    )
    executions.append({
        "tool": write_call["name"], "arguments": write_call["args"],
        "result": write_result, "compact_result": write_envelope.compact_result,
        "projection": {
            **write_envelope.metadata(),
            "lineage_source_tool": "create_google_sheet",
            "lineage_target_tool": "write_google_sheet",
            "lineage_field": "spreadsheet_id",
            "lineage_bound": True,
        },
    })
    tool_results = [
        create_envelope.compact_result, write_envelope.compact_result,
    ]
    if not isinstance(write_result, dict) or write_result.get("error"):
        return {
            "output": "", "tool_results": tool_results,
            "tool_executions": executions, "task_complete": False,
            "error": (
                f"{safe_write_failure_message('write_google_sheet', write_result)}; "
                "automatic repetition is blocked."
            ),
            "error_category": "tool_failure",
            "error_component": "deterministic_sheet_workflow",
            "error_boundary": "sheet_population",
            "error_evidence": {
                "create_succeeded": True, "population_succeeded": False,
                "lineage_bound": True,
            },
        }
    url = create_result.get("spreadsheetUrl") or (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    )
    return {
        "output": f"Created and populated the sender Sheet: {url}",
        "tool_results": tool_results, "tool_executions": executions,
        "task_complete": True,
    }


async def _store_artifacts(conn, run, step, artifacts):
    for index, artifact in enumerate(artifacts):
        external_id = artifact.get("external_id") or f"url-{index}"
        await conn.execute(
            """INSERT INTO agent_artifacts
               (run_id,step_id,user_id,artifact_type,external_id,url,metadata,
                verification_status,verified_at,safe_to_delete)
               VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,'verified',now(),$8)
               ON CONFLICT(run_id,artifact_type,external_id) DO UPDATE SET
                 url=COALESCE(EXCLUDED.url,agent_artifacts.url),
                 metadata=agent_artifacts.metadata||EXCLUDED.metadata,
                 safe_to_delete=agent_artifacts.safe_to_delete OR EXCLUDED.safe_to_delete,
                 verification_status='verified',verified_at=now()""",
            run["id"], step["id"], run["user_id"], step["service"] or "google_resource",
            external_id, artifact.get("url"),
            json.dumps({"source": "tool_result", **artifact.get("metadata", {})}),
            bool(artifact.get("safe_to_delete")),
        )


async def _execute_step(app, pool, run, step, dependencies):
    run_id = run["id"]
    user_id = run["user_id"]
    await append_event(pool, run_id, user_id, "step_started", step_id=step["id"],
                       phase="execution", message=step["title"])
    step_started = time.perf_counter()
    input_data = step.get("input_data") or {}
    semantic_authorization = input_data.get("semantic_authorization") or {}
    if (
        not step.get("read_only", True)
        and semantic_authorization.get("authorized") is False
    ):
        raise ExecutionFailure(
            "The planned external write lacks current-turn service authority.",
            category="policy_violation", component="durable_worker",
            boundary="semantic_write_authorization",
            evidence={
                "service": step.get("service"),
                "operation": step.get("operation"),
                "authorization_basis": semantic_authorization.get("basis"),
            },
        )
    if step.get("operation") in {"answer_information", "answer_workspace_chat"}:
        if step.get("operation") == "answer_information":
            output = informational_answer(
                run["request"], input_data["informational_intent"],
                input_data["capability_catalog"],
            )
        else:
            output = workspace_chat_answer(
                run["request"], input_data["intent_kind"],
                input_data["capability_catalog"],
                input_data.get("okf_sources", []),
            )
        elapsed_ms = int((time.perf_counter() - step_started) * 1000)
        evidence = "Trusted product-information postconditions passed"
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE agent_run_steps SET status='completed',output_data=$1::jsonb,
                   duration_ms=$2,completed_at=now(),error_category=NULL,error_message=NULL
                   WHERE id=$3""",
                json.dumps({"output": output, "verification": evidence,
                            "source": "registered_capability_catalog"}),
                elapsed_ms, step["id"],
            )
        await append_event(
            pool, run_id, user_id, "verification_succeeded", step_id=step["id"],
            phase="verification", message=evidence,
            payload={"source": "registered_capability_catalog", "model_calls": 0,
                     "tool_calls": 0, "rag_mode": "none"},
        )
        await append_event(
            pool, run_id, user_id, "step_completed", step_id=step["id"],
            phase="execution", message=output,
            payload={"artifact_count": 0, "model_calls": 0, "tool_calls": 0},
        )
        return output
    if step.get("operation") in {"recent_senders", "sender_count"}:
        tools = {tool.name: tool for tool in get_toolsets()["gmail"]}
        tool_name = (
            "list_recent_gmail_senders"
            if step.get("operation") == "recent_senders"
            else "count_gmail_senders"
        )
        tool = tools[tool_name]
        if isinstance(tool, GoogleWorkspaceBaseTool):
            tool.db_pool = pool
        state = {
            "session_id": run["session_id"], "user_id": user_id,
            "run_id": str(run_id), "step_id": str(step["id"]),
        }
        call = {
            "id": f"deterministic-{uuid.uuid4()}",
            "name": tool.name,
            "args": input_data.get("tool_arguments") or {"max_results": 20},
        }
        _, raw_result, envelope = await execute_tool_call(tool, call, state, pool)
        executions = [{
            "tool": tool.name, "arguments": call["args"], "result": raw_result,
            "compact_result": envelope.compact_result,
            "projection": envelope.metadata(),
        }]
        if step.get("operation") == "sender_count":
            count = raw_result.get("unique_sender_count", 0)
            qualifier = "" if raw_result.get("complete") else "at least "
            category = raw_result.get("category")
            period = raw_result.get("period")
            description = {
                "promotions": "promotional ",
                "updates": "update ",
                "forums": "forum ",
            }.get(category, f"{category} " if category else "")
            timeframe = " today" if period == "today" else ""
            output = (
                f"{qualifier}{count} unique Gmail sender"
                f"{'s' if count != 1 else ''} sent {description}mail{timeframe}."
            )
            if not raw_result.get("complete"):
                output += (
                    f" This is a lower bound from the first "
                    f"{raw_result.get('messages_scanned', 0)} matching messages."
                )
        else:
            count = raw_result.get("returned", 0)
            output = (
                f"Found {count} recent Gmail sender"
                f"{'s' if count != 1 else ''}."
            )
        result = {
            "output": output,
            "tool_results": [envelope.compact_result],
            "tool_executions": executions,
            "task_complete": True,
        }
    else:
        result = None
    if (
        result is None
        and step.get("service") == "sheets"
        and step.get("operation") == "create_and_write"
    ):
        result = await _create_recent_senders_sheet(
            pool, run, step, dependencies,
        )
    dependency_text = json.dumps(dependencies, default=str)
    planning_request = input_data.get("request") or run["request"]
    scoped_message = (
        f"Overall request: {planning_request}\n\n"
        f"Execute only the {step['service']} portion now. Do not repeat work from "
        f"completed dependency steps. Dependency outputs: {dependency_text}"
    )
    initial = {
        "message": scoped_message, "session_id": run["session_id"],
        "user_id": user_id, "run_id": str(run_id), "step_id": str(step["id"]),
        "forced_service": step["service"], "messages": [],
        "allowed_tools": (step.get("input_data") or {}).get("allowed_tools", []),
        "operation": step.get("operation") or "execute",
        "requires_write": not step.get("read_only", True),
        "expected_write_tools": [],
        "write_completion_mode": "all",
        "tool_selection_retry_count": 0,
        "risk_level": run["risk_level"],
        "allow_small_fallback": (
            run["risk_level"] == "low" and len((run["plan"] or {}).get("services", [])) <= 1
        ),
    }
    contract = write_contract_for(
        step.get("service"), step.get("operation"), initial["allowed_tools"],
    )
    projected_contract = input_data.get("write_contract") or {}
    if contract:
        initial["expected_write_tools"] = list(contract.required_tools)
        initial["write_completion_mode"] = contract.completion_mode
    elif not step.get("read_only", True):
        raise ExecutionFailure(
            "The durable write step has no valid execution contract.",
            category="planning", component="durable_worker",
            boundary="write_contract",
            evidence={
                "service": step.get("service"),
                "operation": step.get("operation"),
                "projected_required_tools": projected_contract.get("required_tools", []),
            },
        )
    if result is None:
        result = await app.state.agent_graph.ainvoke(
            initial, config={"configurable": {"thread_id": f"{run_id}:{step['step_key']}"}}
        )
    output = result.get("output", "")
    if step.get("operation") == "compose" and not str(output).strip():
        result = {
            **result,
            "task_complete": False,
            "error": "The composition stage returned empty content.",
            "error_category": "postcondition_failure",
            "error_component": "composition_agent",
            "error_boundary": "composition_output",
            "error_evidence": {"output_present": False},
        }
    executions = result.get("tool_executions", [])
    if executions:
        verification = await verify_executions_detailed(
            executions, service=step.get("service"), operation=step.get("operation"),
        )
        verified = verification.passed
        evidence = verification.message
        artifacts = verification.artifacts
        if verified and not result.get("task_complete"):
            verified = False
            evidence = result.get("error") or "The agent did not reach a completed state"
    else:
        verified, evidence, artifacts = verify_step(step, result)
    elapsed_ms = int((time.perf_counter() - step_started) * 1000)
    persisted_executions = _persistable_executions(executions)
    error_category = result.get("error_category") or (
        verification.category if executions and not verified else None
    )
    if error_category == "postcondition_failure":
        postcondition_failures.labels(
            step.get("service") or "unknown",
            step.get("operation") or "unknown",
        ).inc()
    if not verified and error_category:
        evidence = result.get("error") or evidence
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE agent_run_steps SET status=$1,output_data=$2::jsonb,
               duration_ms=$3,completed_at=now(),error_category=$4,error_message=$5
               WHERE id=$6""",
            "completed" if verified else "failed",
            json.dumps({"output": output, "tool_results": result.get("tool_results", []),
                        "tool_executions": persisted_executions,
                        "verification": evidence}, default=str),
            elapsed_ms, None if verified else (error_category or "verification"),
            None if verified else evidence, step["id"],
        )
        await _store_artifacts(conn, run, step, artifacts)
    await append_event(
        pool, run_id, user_id,
        "verification_succeeded" if verified else "verification_failed",
        step_id=step["id"], phase="verification", message=evidence,
        payload={"artifacts": len(artifacts)},
    )
    if not verified:
        raise ExecutionFailure(
            evidence,
            category=error_category or "verification",
            component=result.get("error_component") or "step_executor",
            boundary=result.get("error_boundary") or (
                verification.boundary if executions else "postcondition_verification"
            ),
            evidence=result.get("error_evidence") or (
                verification.evidence if executions else {
                "service": step.get("service"), "operation": step.get("operation"),
                }
            ),
        )
    await append_event(pool, run_id, user_id, "step_completed", step_id=step["id"],
                       phase="execution", message=output,
                       payload={"artifact_count": len(artifacts)})
    return output


async def execute_run(app, pool, run):
    run_id = run["id"]
    user_id = run["user_id"]
    heartbeat = asyncio.create_task(_heartbeat(pool, run_id, run["lease_owner"]))
    credential_token = None
    started = time.perf_counter()
    try:
        plan_steps = (run.get("plan") or {}).get("steps", [])
        informational_only = bool(plan_steps) and all(
            step.get("operation") in {"answer_information", "answer_workspace_chat"}
            for step in plan_steps
        )
        if not informational_only:
            credentials = await load_google_credentials(pool, user_id)
            if credentials is None and not get_settings().allow_dev_auth:
                raise RuntimeError("Google credentials are not connected")
            credential_token = request_google_credentials.set(credentials)
        final_outputs = []
        while True:
            async with pool.acquire() as conn:
                current_status = await conn.fetchval(
                    "SELECT status FROM agent_runs WHERE id=$1", run_id
                )
                if current_status == "cancelled":
                    return
                ready = []
                limit = max(1, min(get_settings().worker_step_concurrency, 8))
                for _ in range(limit):
                    step_row = await _claim_step(conn, run_id)
                    if not step_row:
                        break
                    step = dict(step_row)
                    ready.append((step, await _dependency_context(conn, step)))
                if not ready:
                    pending = await conn.fetchval(
                        "SELECT count(*) FROM agent_run_steps WHERE run_id=$1 AND status='pending'",
                        run_id,
                    )
                    if pending:
                        raise RuntimeError("No executable step: dependency graph is blocked")
                    break
                await conn.execute(
                    "UPDATE agent_runs SET current_step_id=$1,heartbeat_at=now() WHERE id=$2",
                    ready[0][0]["id"], run_id,
                )
            batch_results = await asyncio.gather(*(
                _execute_step(app, pool, run, step, dependencies)
                for step, dependencies in ready
            ), return_exceptions=True)
            final_outputs.extend(
                result for result in batch_results if isinstance(result, str)
            )
            failure = next(
                (result for result in batch_results if isinstance(result, BaseException)), None
            )
            if failure:
                raise failure

        final_output = "\n\n".join(output for output in final_outputs if output)
        async with pool.acquire() as conn:
            steps = [dict(row) for row in await conn.fetch(
                "SELECT * FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no", run_id
            )]
            completion = completion_from_steps(steps)
            usage = await conn.fetchrow(
                """SELECT coalesce(array_agg(DISTINCT model) FILTER(WHERE model IS NOT NULL),'{}') AS models,
                          coalesce(sum(input_tokens),0) AS input_tokens,
                          coalesce(sum(output_tokens),0) AS output_tokens
                   FROM agent_model_calls WHERE run_id=$1""",
                run_id,
            )
            await conn.execute(
                """UPDATE agent_runs SET status='completed',current_phase='completed',
                   result=$1::jsonb,incident_summary='{}'::jsonb,
                   technical_completion=$2,functional_completion=$3,
                   user_visible_completion=$4,side_effect_integrity=$5,
                   error_category=NULL,error_message=NULL,completed_at=now(),heartbeat_at=now(),
                   current_step_id=NULL,lease_owner=NULL,lease_expires_at=NULL,
                   models_used=$6,input_tokens=$7,output_tokens=$8 WHERE id=$9""",
                json.dumps({"output": final_output}, default=str),
                completion["technical_completion"], completion["functional_completion"],
                completion["user_visible_completion"], completion["side_effect_integrity"],
                usage["models"], usage["input_tokens"], usage["output_tokens"], run_id,
            )
        await append_event(pool, run_id, user_id, "run_completed", phase="completed",
                           message=final_output, payload={"task_complete": True})
        await record_run_evaluation(pool, run_id)
        run_transitions.labels("completed").inc()
        run_duration.labels("completed").observe(time.perf_counter() - started)
    except Exception as exc:
        category = classify_error(exc)
        retrying = False
        async with pool.acquire() as conn, conn.transaction():
            running_steps = await conn.fetch(
                "SELECT * FROM agent_run_steps WHERE run_id=$1 AND status='running'",
                run_id,
            )
            if (
                running_steps and all(step["read_only"] for step in running_steps)
                and category in {"network", "rate_limit", "worker"}
                and all(step["attempt_count"] < step["max_attempts"] for step in running_steps)
            ):
                await conn.execute(
                    """UPDATE agent_run_steps SET status='pending',error_category=$1,
                         error_message=$2 WHERE run_id=$3 AND status='running'""",
                    category, str(exc), run_id,
                )
                await conn.execute(
                    """UPDATE agent_runs SET status='queued',current_phase='retry_wait',
                         error_category=$1,error_message=$2,lease_owner=NULL,
                         lease_expires_at=NULL,current_step_id=NULL WHERE id=$3""",
                    category, str(exc), run_id,
                )
                retrying = True
        if retrying:
            await append_event(
                pool, run_id, user_id, "retry_scheduled", phase="recovery",
                message=str(exc), payload={"category": category},
            )
            run_transitions.labels("queued").inc()
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE agent_run_steps SET status='failed',error_category=$1,
                   error_message=$2,completed_at=now()
                   WHERE run_id=$3 AND status='running'""",
                category, str(exc), run_id,
            )
            steps = [dict(row) for row in await conn.fetch(
                "SELECT * FROM agent_run_steps WHERE run_id=$1 ORDER BY sequence_no", run_id
            )]
            completion = completion_from_steps(steps)
            incident = build_incident(steps, category, str(exc))
            terminal_status = (
                "partial" if any(step["status"] == "completed" for step in steps)
                else "failed"
            )
            usage = await conn.fetchrow(
                """SELECT coalesce(array_agg(DISTINCT model) FILTER(WHERE model IS NOT NULL),'{}') AS models,
                          coalesce(sum(input_tokens),0) AS input_tokens,
                          coalesce(sum(output_tokens),0) AS output_tokens
                   FROM agent_model_calls WHERE run_id=$1""",
                run_id,
            )
            await conn.execute(
                """UPDATE agent_runs SET status=$1,current_phase=$1,
                   incident_summary=$2::jsonb,technical_completion=$3,
                   functional_completion=$4,user_visible_completion=$5,
                   side_effect_integrity=$6,error_category=$7,error_message=$8,
                   completed_at=now(),lease_owner=NULL,lease_expires_at=NULL,
                   models_used=$9,input_tokens=$10,output_tokens=$11 WHERE id=$12""",
                terminal_status, json.dumps(incident), completion["technical_completion"],
                completion["functional_completion"], completion["user_visible_completion"],
                completion["side_effect_integrity"], category, str(exc),
                usage["models"], usage["input_tokens"], usage["output_tokens"], run_id,
            )
        await append_event(
            pool, run_id, user_id, f"run_{terminal_status}", phase=terminal_status,
            message=str(exc), payload={"category": category},
        )
        failed_step = next((step for step in steps if step["status"] == "failed"), None)
        try:
            failure_record = await record_failure_incident(
                pool, occurrence_key=f"run:{run_id}:terminal", run_id=run_id,
                session_id=run["session_id"], user_id=user_id,
                message=run["request"], intent_kind=run.get("intent_kind") or "workspace_action",
                stage=(
                    "verification"
                    if category in {"verification", "postcondition_failure"}
                    else "execution"
                ),
                category=category,
                component=getattr(exc, "component", "durable_worker"), error=str(exc),
                service=failed_step["service"] if failed_step else None,
                operation=failed_step["operation"] if failed_step else None,
                breaking_point=(failed_step["title"] if failed_step else incident.get("breaking_point")),
                completion=completion,
                evidence={"run_event": f"run_{terminal_status}",
                          "step_key": failed_step["step_key"] if failed_step else None,
                          "boundary": getattr(exc, "boundary", None),
                          **getattr(exc, "evidence", {})},
                policy=run.get("plan") or {},
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agent_runs SET failure_fingerprint=$1 WHERE id=$2",
                    failure_record["failure_fingerprint"], run_id,
                )
        except Exception:
            # Failure intelligence is best effort and must never replace the run outcome.
            logger.exception("Unable to persist failure intelligence for run %s", run_id)
        await record_run_evaluation(pool, run_id)
        run_transitions.labels(terminal_status).inc()
        run_failures.labels(category).inc()
        run_duration.labels(terminal_status).observe(time.perf_counter() - started)
    finally:
        if credential_token is not None:
            request_google_credentials.reset(credential_token)
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def worker_loop(app, pool, stop_event: asyncio.Event):
    owner = f"{socket.gethostname()}:{id(asyncio.current_task())}"
    while not stop_event.is_set():
        run = await claim_run(pool, owner)
        if run:
            if run.get("_terminal_recovery"):
                continue
            await execute_run(app, pool, run)
            continue
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=get_settings().worker_poll_seconds
            )
        except asyncio.TimeoutError:
            pass
