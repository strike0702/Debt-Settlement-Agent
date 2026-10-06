"""Application settings from environment / ``.env`` (pydantic-settings).

API keys, LLM profile and per-role request timeouts, NLG/NLU mode, DB path,
negotiation knobs (max turns, anchor ratio, firm disclosure). Call
``get_settings()``; do not construct ``Settings`` ad hoc in hot paths.
"""

from __future__ import annotations

from functools import lru_cache

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
    # Per-request LLM timeouts (s). A timeout fails over to the next route target.
    llm_timeout_nlu_s: float = 6.0
    llm_timeout_nlg_s: float = 4.0
    llm_timeout_stt_s: float = 8.0
    # Sim phrasing is eval-only and not on the voice path; looser bound.
    llm_timeout_sim_s: float = 15.0

    db_path: str = "debt_settlement_agent.db"

    hostility_threshold: float = 0.8
    max_turns: int = 24
    anchor_ratio: float = 0.7
    concession_factor: float = 0.5
    max_counters: int = 4
    # Confirm at ask when gap to next counter is this small or less (bp).
    close_gap_bp: int = 200

    firm_name: str = "Synthetic Debt Relief"
    opening_disclosure: str = (
        "I am authorized to discuss settlement options for this account."
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide settings (env read once)."""
    return Settings()
