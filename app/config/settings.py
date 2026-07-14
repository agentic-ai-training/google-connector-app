from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    groq_api_key: str = ""
    deepseek_api_key: str = ""
    database_url: str = "postgresql://agent_user:agent_pass_2024@localhost:5432/agent_db"
    async_database_url: str = "postgresql+asyncpg://agent_user:agent_pass_2024@localhost:5432/agent_db"
    langchain_tracing_v2: str = "true"
    langchain_api_key: str = ""
    langchain_project: str = "google-agent"
    google_credentials_path: str = "./credentials.json"
    jwt_secret_key: str = "change-this-in-production-use-256-bit-random-string"
    jwt_algorithm: str = "HS256"
    admin_emails: str = "achintyat256@gmail.com"
    railway_url: str = ""
    neon_database_url: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

@lru_cache
def get_settings() -> Settings:
    return Settings()
