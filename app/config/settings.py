from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    groq_api_key: str = ""
    groq_fast_model: str = "llama-3.3-70b-versatile"
    groq_reasoning_model: str = "openai/gpt-oss-120b"
    groq_fallback_model: str = "llama-3.1-8b-instant"
    groq_max_tokens: int = 800
    database_url: str = "postgresql://agent_user:agent_pass_2024@localhost:5432/agent_db"
    async_database_url: str = "postgresql+asyncpg://agent_user:agent_pass_2024@localhost:5432/agent_db"
    langchain_tracing_v2: str = "true"
    langchain_api_key: str = ""
    langchain_project: str = "google-agent"
    langsmith_api_key: str = ""
    langsmith_tracing: str = "true"
    langsmith_project: str = "google-agent"
    google_credentials_path: str = "./credentials.json"
    google_token_path: str = "./token.pkl"
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
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    neon_database_url: str = ""
    ollama_host: str = "http://localhost:11434"
    durable_runs_enabled: bool = True
    embedded_worker_enabled: bool = True
    legacy_chat_enabled: bool = True
    okf_enabled: bool = True
    governed_improvements_enabled: bool = True
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 120
    worker_step_concurrency: int = 3
    max_active_runs_per_user: int = 3
    max_runs_per_user_hour: int = 60
    max_active_runs_global: int = 100
    max_request_chars: int = 12000
    raw_telemetry_retention_days: int = 14
    workflow_retention_days: int = 90
    aggregate_retention_days: int = 365
    admin_notification_email: str = ""
    grafana_cloud_prometheus_url: str = ""
    grafana_cloud_prometheus_username: str = ""
    grafana_cloud_api_key: str = ""
    deployment_version: str = "local"
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"), extra="ignore"
    )

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
