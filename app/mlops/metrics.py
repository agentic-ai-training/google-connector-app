from prometheus_client import Counter, Histogram
tool_errors = Counter("agent_tool_errors_total", "Tool errors", ["tool_name"])
tool_latency = Histogram("agent_tool_latency_seconds", "Tool latency", ["tool_name"])
llm_latency = Histogram("agent_llm_latency_seconds", "LLM latency", ["model"])
empty_context = Counter("agent_empty_context_total", "Empty RAG retrievals")
request_count = Counter("agent_requests_total", "Total requests", ["endpoint"])
request_latency = Histogram(
    "agent_request_latency_seconds",
    "Time until an HTTP response starts",
    ["endpoint", "method", "status"],
)
