"""Application settings loaded from environment / .env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM / STT keys (provider skipped when unset)
    groq_api_key: str | None = None
    mistral_api_key: str | None = None
    gemini_api_key: str | None = None
    openrouter_api_key: str | None = None
    cerebras_api_key: str | None = None

    llm_profile: str = "demo"
    llm_cache: bool = True
    llm_cache_path: str = "llm_cache.db"
    nlg_mode: str = "llm"
    nlu_mode: str = "llm"

    db_path: str = "debt_settlement_agent.db"

    hostility_threshold: float = 0.8
    max_turns: int = 24
    anchor_ratio: float = 0.7
    concession_factor: float = 0.5
    max_counters: int = 4

    firm_name: str = "Synthetic Debt Relief"
    opening_disclosure: str = (
        "This call uses synthetic data for demonstration only. "
        "You are speaking with an automated agent authorized to discuss settlement options."
    )


def get_settings() -> Settings:
    return Settings()
