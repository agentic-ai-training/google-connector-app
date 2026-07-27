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
failure_incidents = Counter(
    "agent_failure_incidents_total",
    "Durable failures captured for administrator analysis",
    ["stage", "category"],
)
failure_review_queue = Gauge(
    "agent_failure_review_queue",
    "Failure incidents awaiting a human decision",
    ["stage", "risk"],
)
failure_notifications = Gauge(
    "agent_failure_notifications",
    "Failure intelligence notifications by channel and state",
    ["channel", "status"],
)
tool_result_tokens = Histogram(
    "agent_tool_result_projected_tokens",
    "Estimated tokens retained after deterministic tool-result projection",
    ["tool_name", "truncated"],
    buckets=(16, 32, 64, 128, 256, 512, 1024, 2000, 4000),
)
tool_result_bytes_removed = Counter(
    "agent_tool_result_bytes_removed_total",
    "Bytes removed before a tool result enters model or durable dependency context",
    ["tool_name"],
)
model_context_preflight_tokens = Histogram(
    "agent_model_context_preflight_tokens",
    "Estimated input plus tool-schema tokens before provider calls",
    ["model"],
    buckets=(1000, 2000, 4000, 8000, 16000, 24000, 32000, 64000),
)
model_context_preflight_compactions = Counter(
    "agent_model_context_preflight_compactions_total",
    "Earlier tool payloads compacted before provider calls",
    ["model"],
)
candidate_build_queue = Gauge(
    "agent_candidate_builds", "Groq-only candidate builds by durable state", ["status"],
)
tool_selection_corrections = Counter(
    "agent_tool_selection_corrections_total",
    "Write steps given one constrained missing-tool correction",
    ["service", "operation"],
)
tool_selection_failures = Counter(
    "agent_tool_selection_failures_total",
    "Write steps terminated because required tools were not selected",
    ["service", "operation"],
)
postcondition_failures = Counter(
    "agent_postcondition_failures_total",
    "Write readback mismatches by stable operation boundary",
    ["service", "operation"],
)
write_reconciliation = Counter(
    "agent_write_reconciliation_total",
    "Write reconciliation decisions",
    ["service", "operation", "outcome"],
)
candidate_progress_gates = Counter(
    "agent_candidate_progress_gates_total",
    "Candidate builder progress gates",
    ["role", "gate"],
)
candidate_checkpoint_resumes = Counter(
    "agent_candidate_checkpoint_resumes_total",
    "Candidate builder checkpoint resumes",
    ["role", "phase"],
)
candidate_budget_ratio = Gauge(
    "agent_candidate_budget_ratio",
    "Cumulative candidate tokens divided by effective budget",
    ["mode", "status"],
)
candidate_progress_state = Gauge(
    "agent_candidate_progress_state",
    "Candidate builds at each bounded role and progress gate",
    ["role", "gate"],
)
candidate_retry_state = Gauge(
    "agent_candidate_retry_state",
    "Candidate builds by server-authoritative retry outcome",
    ["eligible", "reason"],
)
failure_theme_queue = Gauge(
    "agent_failure_themes", "Cross-cluster architectural themes by state", ["status"],
)
canary_routing = Gauge(
    "agent_canary_routing", "Governed canaries by routing and lifecycle state",
    ["status", "routing_enabled"],
)
okf_bundle_publications = Gauge(
    "agent_okf_bundle_publications",
    "Immutable OKF bundles by governed publication state", ["status"],
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
rag_quality_samples = Gauge(
    "agent_rag_quality_samples",
    "Number of valid RAG evaluation samples in the rolling quality window",
)
build_info = Info("agent_build", "Immutable deployed application version")
