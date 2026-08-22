from functools import lru_cache
import re
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_JWT_SECRETS = {
    "",
    "change-this-in-production-use-256-bit-random-string",
}

class Settings(BaseSettings):
    runtime_model_provider: str = "gemini"
    runtime_api_key: str = ""
    runtime_fast_model: str = "gemini-2.5-flash"
    runtime_reasoning_model: str = "gemini-2.5-pro"
    runtime_fallback_model: str = "gemini-2.5-flash-lite"
    runtime_max_tokens: int = 800
    runtime_composition_max_tokens: int = 4000
    runtime_context_window_tokens: int = 32768
    runtime_tool_result_max_tokens: int = 2000
    runtime_context_safety_tokens: int = 1024
    coding_groq_api_key: str = ""
    coding_agent_enabled: bool = False
    coding_worker_enabled: bool = False
    candidate_builder_use_coding_runtime: bool = True
    coding_agent_admin_only: bool = True
    coding_allowed_repositories: str = ""
    coding_local_runner_binary: str = "/usr/local/bin/gca-local"
    coding_planner_model: str = "llama-3.3-70b-versatile"
    coding_model_policy_version: str = "groq-tool-planner-v1"
    coding_tool_policy_version: str = "rust-broker-v2"
    coding_worker_poll_seconds: float = 2.0
    coding_worker_lease_seconds: int = 300
    coding_approval_ttl_hours: int = 24
    coding_ci_timeout_minutes: int = 60
    coding_run_retention_days: int = 90
    private_tool_result_max_bytes: int = 2_000_000
    private_tool_result_retention_hours: int = 24
    database_url: str = "postgresql://agent_user:agent_pass_2024@localhost:5432/agent_db"
    async_database_url: str = "postgresql+asyncpg://agent_user:agent_pass_2024@localhost:5432/agent_db"
    langchain_tracing_v2: str = "true"
    langchain_api_key: str = ""
    langchain_project: str = "google-agent"
    langsmith_api_key: str = ""
    langsmith_tracing: str = "true"
    langsmith_project: str = "google-agent"
    google_credentials_path: str = "./credentials.json"
    google_token_json: str = ""
    google_oauth_client_json: str = ""
    google_oauth_client_path: str = "./google-oauth-web.json"
    frontend_url: str = "http://localhost:3000"
    google_oauth_redirect_uri: str = ""
    allow_dev_auth: bool = False
    jwt_secret_key: str = "change-this-in-production-use-256-bit-random-string"
    jwt_algorithm: str = "HS256"
    oauth_encryption_keys: str = ""
    admin_emails: str = "achintyat256@gmail.com"
    railway_url: str = ""
    railway_public_domain: str = ""
    railway_project_id: str = ""
    railway_candidate_project_id: str = ""
    railway_candidate_worker_service: str = "google-connector-app"
    candidate_api_request_timeout_seconds: float = 20.0
    candidate_worker_rag_enabled: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    neon_database_url: str = ""
    ollama_host: str = "http://localhost:11434"
    durable_runs_enabled: bool = True
    embedded_worker_enabled: bool = True
    legacy_chat_enabled: bool = True
    okf_enabled: bool = True
    okf_private_bundle_path: str = ""
    governed_improvements_enabled: bool = True
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 120
    worker_step_concurrency: int = 3
    max_active_runs_per_user: int = 3
    max_runs_per_user_hour: int = 60
    max_active_runs_global: int = 100
    max_request_chars: int = 12000
    max_embedding_jobs_global: int = 5000
    max_embedding_jobs_per_user: int = 500
    max_embedding_payload_chars: int = 250000
    rag_query_embedding_timeout_seconds: float = 8.0
    runtime_daily_token_budget: int = 100000
    runtime_quality_reserve_tokens: int = 15000
    candidate_builder_enabled: bool = True
    candidate_builder_model: str = "llama-3.3-70b-versatile"
    candidate_builder_fallback_models: str = (
        "openai/gpt-oss-120b,qwen/qwen3.6-27b,openai/gpt-oss-20b"
    )
    candidate_builder_job_token_budget: int = 12000
    candidate_builder_max_effective_token_budget: int = 48000
    candidate_builder_max_output_tokens: int = 6000
    candidate_builder_poll_seconds: float = 5.0
    candidate_builder_timeout_seconds: int = 240
    candidate_ci_attestation_token: str = ""
    candidate_deploy_attestation_token: str = ""
    candidate_builder_callback_token: str = ""
    raw_telemetry_retention_days: int = 14
    workflow_retention_days: int = 90
    aggregate_retention_days: int = 365
    admin_notification_email: str = ""
    github_proposal_repository: str = "agentic-ai-training/google-connector-app"
    github_coding_app_id: str = ""
    github_coding_app_installation_id: str = ""
    github_coding_app_private_key: str = ""
    github_proposal_token: str = ""
    grafana_cloud_prometheus_url: str = ""
    grafana_cloud_prometheus_username: str = ""
    grafana_cloud_api_key: str = ""
    otel_enabled: bool = True
    otel_service_name: str = ""
    otel_exporter_otlp_endpoint: str = ""
    otel_exporter_otlp_headers: str = ""
    deployment_version: str = "local"
    railway_git_commit_sha: str = ""
    executor_version: str = ""
    executor_role: str = "control"
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"), extra="ignore"
    )

    @model_validator(mode="after")
    def prefer_immutable_railway_source_version(self):
        """Bind runtime evidence to the image Railway actually built.

        A manually managed DEPLOYMENT_VERSION can become stale when Railway's native
        GitHub integration deploys a new commit. Railway injects this SHA into every
        repository-backed deployment, so it is the authoritative control version.
        """
        if self.railway_git_commit_sha:
            if not re.fullmatch(r"[0-9a-f]{40}", self.railway_git_commit_sha):
                raise ValueError("RAILWAY_GIT_COMMIT_SHA must be a complete Git SHA")
            self.deployment_version = self.railway_git_commit_sha
        return self

    @model_validator(mode="after")
    def enforce_runtime_provider_boundary(self):
        """Keep Groq credentials exclusive to coding and candidate execution."""
        provider = self.runtime_model_provider.strip().casefold()
        if provider != "gemini":
            raise ValueError("RUNTIME_MODEL_PROVIDER must be gemini")
        if self.runtime_api_key and self.runtime_api_key == self.coding_groq_api_key:
            raise ValueError("Runtime and coding provider credentials must be distinct")
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_public_url() -> str:
    settings = get_settings()
    if settings.railway_url:
        return settings.railway_url.rstrip("/")
    if settings.railway_public_domain:
        return f"https://{settings.railway_public_domain.strip('/')}"
    return ""


def validate_runtime_security(settings: Settings) -> None:
    """Refuse production startup when authentication cannot be trusted."""
    secret = settings.jwt_secret_key.strip()
    if not settings.allow_dev_auth and (
        secret in INSECURE_JWT_SECRETS or len(secret.encode("utf-8")) < 32
    ):
        raise RuntimeError(
            "JWT_SECRET_KEY must be a non-placeholder secret of at least 32 bytes "
            "when ALLOW_DEV_AUTH is false"
        )
