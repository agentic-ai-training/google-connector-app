import json
from typing import Any
from langchain_core.tools import BaseTool
from pydantic import ConfigDict
from app.mlops.metrics import tool_errors, tool_latency

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
