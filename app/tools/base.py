import asyncio
import json
import time
from contextvars import ContextVar
from typing import Any
from langchain_core.tools import BaseTool
from pydantic import ConfigDict
from app.mlops.metrics import tool_errors, tool_latency

tool_session_id: ContextVar[str | None] = ContextVar("tool_session_id", default=None)
tool_user_id: ContextVar[str | None] = ContextVar("tool_user_id", default=None)
tool_run_id: ContextVar[str | None] = ContextVar("tool_run_id", default=None)
tool_step_id: ContextVar[str | None] = ContextVar("tool_step_id", default=None)
_persistence_tasks: set[asyncio.Task] = set()


async def _persist_safely(name, kwargs, result, pool, embedder, user_id):
    try:
        from app.rag.jobs import enqueue_tool_result
        await enqueue_tool_result(name, kwargs, result, pool, user_id)
    except Exception:
        # Live tool success must not be converted into failure by optional indexing.
        # The durable ingestion worker and metrics own retries/reporting.
        return

class GoogleWorkspaceBaseTool(BaseTool):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    db_pool: Any = None
    embedder: Any = None

    async def _log_task(self, session_id, tool_name, input_data, output_data, status,
                        error_msg=None, llm_latency_ms=None, total_latency_ms=None, model_used=None):
        if not self.db_pool:
            return
        async with self.db_pool.acquire() as conn:
            await conn.execute("""INSERT INTO task_log(session_id,tool_name,input_data,output_data,status,error_message,llm_latency_ms,total_latency_ms,model_used)
                VALUES($1,$2,$3::jsonb,$4::jsonb,$5,$6,$7,$8,$9)""", session_id, tool_name,
                json.dumps(input_data, default=str), json.dumps(output_data, default=str), status,
                error_msg, llm_latency_ms, total_latency_ms, model_used)

    async def _embed_and_upsert(self, table, id_val, text, extra_fields):
        if not self.embedder or not self.db_pool:
            return
        if table not in {"gmail_messages", "calendar_events", "drive_documents", "contacts", "chat_messages"}:
            raise ValueError("Unsupported embedding table")
        embedding = await self.embedder.aembed_query(text)
        keys = list(extra_fields)
        columns = ",".join(["id", "embedding", *keys])
        values = ",".join(f"${i}" for i in range(1, len(keys)+3))
        updates = ",".join(["embedding=EXCLUDED.embedding", *(f"{k}=EXCLUDED.{k}" for k in keys), "synced_at=now()"])
        async with self.db_pool.acquire() as conn:
            await conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({values}) ON CONFLICT(id) DO UPDATE SET {updates}", id_val, embedding, *extra_fields.values())

    def _track_metric(self, elapsed, error=False):
        tool_latency.labels(self.name).observe(elapsed)
        if error:
            tool_errors.labels(self.name).inc()


class GoogleWorkspaceTool(GoogleWorkspaceBaseTool):
    """Instrumented adapter that gives every concrete tool the shared base behavior."""

    wrapped: Any

    def _run(self, **kwargs):
        started = time.perf_counter()
        try:
            result = self.wrapped.invoke(kwargs)
            self._track_metric(time.perf_counter() - started)
            return result
        except Exception:
            self._track_metric(time.perf_counter() - started, error=True)
            raise

    async def _arun(self, **kwargs):
        started = time.perf_counter()
        try:
            result = await self.wrapped.ainvoke(kwargs)
            elapsed = time.perf_counter() - started
            self._track_metric(elapsed)
            if self.db_pool and self.embedder:
                task = asyncio.create_task(
                    _persist_safely(
                        self.name, kwargs, result, self.db_pool, self.embedder,
                        tool_user_id.get(),
                    )
                )
                _persistence_tasks.add(task)
                task.add_done_callback(_persistence_tasks.discard)
            await self._log_task(
                tool_session_id.get(), self.name, kwargs, result,
                "success", total_latency_ms=int(elapsed * 1000),
            )
            return result
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self._track_metric(elapsed, error=True)
            await self._log_task(
                tool_session_id.get(), self.name, kwargs, {}, "error",
                error_msg=str(exc), total_latency_ms=int(elapsed * 1000),
            )
            raise


def instrument_tool(tool: BaseTool) -> GoogleWorkspaceTool:
    return GoogleWorkspaceTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        wrapped=tool,
    )
