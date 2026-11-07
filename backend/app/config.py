from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "IncidentIQ"
    api_prefix: str = "/api"
    database_url: str = "sqlite:///./incidentiq.db"
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    hindsight_base_url: str | None = None
    hindsight_api_key: str | None = None
    hindsight_bank_id: str = "incidentiq-memory"
    incidentiq_approval_token: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
