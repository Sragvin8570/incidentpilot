from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    jwt_secret: SecretStr
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: SecretStr | None = None
    llm_model: str = "gemini-3.5-flash-lite"
    investigation_timeout_seconds: int = Field(default=300, gt=0)
    runbook_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    prometheus_url: str = "http://localhost:9090"
    alertmanager_webhook_token: SecretStr | None = None
    approval_expiry_minutes: int = Field(default=30, gt=0)
    execution_timeout_seconds: int = Field(default=300, gt=0)
    executor_image: str = "incidentpilot-executor:local"

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
