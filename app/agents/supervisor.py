import asyncio
import json
import re
import time
import uuid
from collections.abc import Sequence

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, StateGraph

from app.agents.router import get_llm, route_model_node
from app.agents.state import AgentState
from app.mlops.metrics import llm_latency, tool_errors, tool_latency
from app.tools.base import GoogleWorkspaceBaseTool, tool_session_id
from app.rag.context_packer import pack_context
from app.rag.retriever import hybrid_retrieve

SERVICES = ("gmail", "calendar", "drive", "docs", "sheets", "tasks", "chat", "contacts", "meet")
ALIASES = {
    "email": "gmail",
    "mail": "gmail",
    "event": "calendar",
    "meeting": "calendar",
    "file": "drive",
    "document": "docs",
    "spreadsheet": "sheets",
    "task": "tasks",
    "contact": "contacts",
    "video call": "meet",
    "google meet": "meet",
}

FAILED_TOOL_PATTERN = re.compile(
    r"<function=([A-Za-z0-9_]+)(\{.*?\})</function>", re.DOTALL
)


def recover_rejected_tool_call(exc: Exception) -> AIMessage | None:
    """Recover the structured call Groq includes with tool_use_failed errors."""
    match = FAILED_TOOL_PATTERN.search(str(exc))
    if not match:
        return None
    try:
        arguments = json.loads(match.group(2))
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    return AIMessage(
        content="",
        tool_calls=[{
            "name": match.group(1),
            "args": arguments,
            "id": f"recovered-{uuid.uuid4()}",
            "type": "tool_call",
        }],
    )


def get_toolsets() -> dict[str, list[BaseTool]]:
    from app.tools import registry as tools

    return {
        "gmail": [tools.search_gmail, tools.get_gmail_message, tools.send_gmail,
                  tools.reply_gmail, tools.label_gmail, tools.trash_gmail,
                  tools.list_gmail_threads],
        "calendar": [tools.list_calendar_events, tools.get_calendar_event,
                     tools.create_calendar_event, tools.update_calendar_event,
                     tools.delete_calendar_event, tools.check_calendar_availability],
        "drive": [tools.search_drive, tools.get_drive_file, tools.upload_drive_file,
                  tools.share_drive_file, tools.move_drive_file],
        "docs": [tools.read_google_doc, tools.create_google_doc,
                 tools.append_to_google_doc],
        "sheets": [tools.read_google_sheet, tools.write_google_sheet,
                   tools.append_to_google_sheet, tools.create_google_sheet],
        "tasks": [tools.list_tasks, tools.create_task, tools.complete_task],
        "contacts": [tools.search_contacts, tools.get_contact],
        "chat": [tools.list_chat_spaces, tools.send_chat_message],
        "meet": [tools.create_meet_space, tools.get_meet_space,
                 tools.list_meet_conferences, tools.list_meet_participants],
    }


async def retrieve_context_node(state: AgentState):
    try:
        # Railway's small Ollama service can take over a minute to cold-start.
        # RAG is optional context, so never let a cold embedding model block chat.
        async with asyncio.timeout(20):
            docs = await hybrid_retrieve(state.get("message", ""))
    except Exception as exc:
        docs = []
        return {"retrieved_context": "", "tool_results": [], "error": str(exc)}
    return {"retrieved_context": pack_context(docs), "tool_results": docs}


async def supervisor_node(state: AgentState):
    text = state.get("message", "").lower()
    selected = [service for service in SERVICES if service in text]
    selected.extend(value for key, value in ALIASES.items() if key in text)
    selected = list(dict.fromkeys(selected)) or ["gmail"]
    return {"service": selected[0], "services": selected}


def route_to_subagent(state: AgentState):
    return state.get("service", "error")


async def _record_tool_call(pool, session_id, tool_name, args, result, status,
                            elapsed_ms, error=None):
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO task_log
               (session_id,tool_name,input_data,output_data,status,error_message,
                total_latency_ms)
               VALUES($1,$2,$3::jsonb,$4::jsonb,$5,$6,$7)""",
            session_id,
            tool_name,
            json.dumps(args, default=str),
            json.dumps(result, default=str),
            status,
            error,
            elapsed_ms,
        )


async def _execute_tool(tool: BaseTool, call: dict, state: AgentState, pool):
    started = time.perf_counter()
    context_token = tool_session_id.set(state.get("session_id"))
    try:
        result = await tool.ainvoke(call.get("args", {}))
        elapsed = int((time.perf_counter() - started) * 1000)
        if not isinstance(tool, GoogleWorkspaceBaseTool):
            tool_latency.labels(tool.name).observe(elapsed / 1000)
        if not isinstance(tool, GoogleWorkspaceBaseTool):
            await _record_tool_call(pool, state.get("session_id"), tool.name,
                                    call.get("args", {}), result, "success", elapsed)
        return ToolMessage(
            content=json.dumps(result, default=str),
            tool_call_id=call["id"],
            name=tool.name,
        ), result
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        if not isinstance(tool, GoogleWorkspaceBaseTool):
            tool_errors.labels(tool.name).inc()
            tool_latency.labels(tool.name).observe(elapsed / 1000)
        if not isinstance(tool, GoogleWorkspaceBaseTool):
            await _record_tool_call(pool, state.get("session_id"), tool.name,
                                    call.get("args", {}), {}, "error", elapsed,
                                    str(exc))
        return ToolMessage(
            content=f"Tool error: {exc}",
            tool_call_id=call["id"],
            name=tool.name,
            status="error",
        ), {"error": str(exc), "tool": tool.name}
    finally:
        tool_session_id.reset(context_token)


def make_service_node(service: str, pool=None):
    async def service_node(state: AgentState):
        try:
            toolsets = get_toolsets()
            if pool:
                from app.rag.embedder import NomicEmbedder
                embedder = NomicEmbedder()
                for group in toolsets.values():
                    for tool in group:
                        if isinstance(tool, GoogleWorkspaceBaseTool):
                            tool.db_pool = pool
                            tool.embedder = embedder
            chosen = state.get("services") or [service]
            available = [tool for name in chosen for tool in toolsets[name]]
            # Multi-service commands may discover an additional needed service;
            # expose the full surface while preserving the supervisor classification.
            if len(chosen) > 1:
                available = [tool for group in toolsets.values() for tool in group]
            by_name = {tool.name: tool for tool in available}
            model_choice = state.get("model_to_use", "groq_fast")
            llm = get_llm(model_choice).bind_tools(available)
            context = state.get("retrieved_context", "")
            system = state.get("system_prompt") or (
                "You are a precise Google Workspace automation agent. Plan before "
                "acting, call tools sequentially, verify every result, and never claim "
                "an action succeeded unless its tool result confirms success."
            )
            messages = [
                SystemMessage(content=f"{system}\n\nRetrieved context:\n{context}"),
                HumanMessage(content=state.get("message", "")),
            ]
            results = []
            for _ in range(8):
                for attempt in range(2):
                    llm_started = time.perf_counter()
                    try:
                        response = await llm.ainvoke(messages)
                        break
                    except Exception as exc:
                        error_text = str(exc).lower()
                        if "rate_limit" in error_text or "rate limit" in error_text:
                            llm = get_llm(model_choice, fallback=True).bind_tools(available)
                            response = await llm.ainvoke(messages)
                            break
                        if "tool_use_failed" not in error_text:
                            raise
                        if attempt:
                            response = recover_rejected_tool_call(exc)
                            if response is None:
                                raise
                    finally:
                        llm_latency.labels(
                            state.get("model_to_use", "groq_fast")
                        ).observe(time.perf_counter() - llm_started)
                messages.append(response)
                calls = getattr(response, "tool_calls", [])
                if not calls:
                    return {
                        "messages": messages[1:],
                        "output": str(response.content),
                        "tool_results": results,
                        "task_complete": True,
                    }
                for call in calls:
                    tool = by_name.get(call["name"])
                    if not tool:
                        message = ToolMessage(
                            content=f"Unknown tool: {call['name']}",
                            tool_call_id=call["id"],
                            name=call["name"],
                            status="error",
                        )
                        result = {"error": "unknown tool", "tool": call["name"]}
                    else:
                        message, result = await _execute_tool(tool, call, state, pool)
                    messages.append(message)
                    results.append(result)
            raise RuntimeError("Tool-call limit reached before the task completed")
        except Exception as exc:
            return {
                "error": str(exc),
                "output": f"I couldn't complete that request: {exc}",
                "task_complete": False,
            }

    service_node.__name__ = f"{service}_agent"
    return service_node


async def error_handler_node(state: AgentState):
    return {
        "output": f"I couldn't complete that request: {state.get('error', 'unknown error')}",
        "task_complete": False,
    }


async def respond_node(state: AgentState):
    return {"output": state.get("output", "")}


def build_agent_graph(pool=None, checkpointer=None):
    graph = StateGraph(AgentState)
    graph.add_node("route_model", route_model_node)
    graph.add_node("retrieve_context", retrieve_context_node)
    graph.add_node("supervisor", supervisor_node)
    for name in SERVICES:
        graph.add_node(f"{name}_agent", make_service_node(name, pool))
    graph.add_node("error_handler", error_handler_node)
    graph.add_node("respond", respond_node)
    graph.set_entry_point("route_model")
    graph.add_edge("route_model", "retrieve_context")
    graph.add_edge("retrieve_context", "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_to_subagent,
        {**{service: f"{service}_agent" for service in SERVICES},
         "error": "error_handler"},
    )
    for name in SERVICES:
        graph.add_edge(f"{name}_agent", "respond")
    graph.add_edge("error_handler", "respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer)
