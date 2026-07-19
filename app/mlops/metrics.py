from prometheus_client import Counter, Gauge, Histogram, Info
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
run_transitions = Counter(
    "agent_run_transitions_total", "Durable run state transitions", ["status"]
)
run_failures = Counter(
    "agent_run_failures_total", "Durable run failures", ["category"]
)
run_duration = Histogram(
    "agent_run_duration_seconds", "End-to-end durable run duration", ["status"]
)
run_queue_depth = Gauge(
    "agent_run_queue_depth", "Runs currently queued or leased", ["status"]
)
approval_requests = Counter(
    "agent_approval_requests_total", "High-risk approvals requested", ["risk"]
)
rag_decisions = Counter(
    "agent_rag_decisions_total", "RAG gate decisions", ["mode", "reason"]
)
stale_runs = Gauge("agent_stale_runs", "Running jobs with an expired worker lease")
embedding_queue = Gauge(
    "agent_embedding_jobs", "Embedding jobs by durable state", ["status"]
)
improvement_queue = Gauge(
    "agent_improvement_proposals", "Governed proposals by lifecycle state", ["status"]
)
artifact_cleanup_queue = Gauge(
    "agent_artifact_cleanup_requests", "Artifact compensation requests by state", ["status"]
)
improvement_notifications = Gauge(
    "agent_improvement_notifications", "Improvement notifications by channel and state",
    ["channel", "status"],
)
embedding_duration = Histogram(
    "agent_embedding_duration_seconds", "Ollama embedding latency", ["operation", "status"]
)
embedding_input_chars = Histogram(
    "agent_embedding_input_chars", "Characters submitted to the embedding model",
    ["operation"], buckets=(100, 500, 1000, 2000, 4000, 6000, 12000, 24000),
)
embedding_overflows = Counter(
    "agent_embedding_context_overflows_total", "Embedding context-overflow retries"
)
ollama_model_loaded = Gauge(
    "agent_ollama_model_loaded", "Whether the configured embedding model answered successfully"
)
embedding_admission_rejections = Counter(
    "agent_embedding_admission_rejections_total",
    "Embedding persistence jobs rejected before queueing",
    ["reason"],
)
oauth_outcomes = Counter(
    "agent_oauth_outcomes_total", "Google OAuth outcomes", ["outcome"]
)
rag_quality = Gauge(
    "agent_rag_quality", "Latest rolling offline RAG quality score", ["metric"]
)
build_info = Info("agent_build", "Immutable deployed application version")
