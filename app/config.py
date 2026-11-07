"""Application settings from environment / ``.env`` (pydantic-settings).

API keys, LLM profile and per-role request timeouts, NLG/NLU mode (and the
NLG template-bank path), DB path,
negotiation knobs (max turns, anchor ratio, accept line, loop guard, firm
disclosure), and the engine's schedule objective and fee policy
(``ENGINE_OBJECTIVE`` / ``ENGINE_FEE_POLICY``, docs/ENGINE.md). Call
``get_settings()``; do not construct ``Settings`` ad hoc in hot paths.

``claude_daily_budget_usd`` (env ``CLAUDE_DAILY_BUDGET_USD``) caps the
demo's paid Claude NLU per UTC day; see ``app.llm.budget``.

Voice is always ``app.voice.pipeline`` (Pipecat over WebRTC, Deepgram Flux STT
and Aura-2 TTS, under ``voice_daily_budget_usd``); the browser-speech path and
its ``VOICE_STACK`` switch were removed in Phase 67.

API key pools: besides the declared ``<provider>_api_key`` fields, every
``*_KEY`` / ``*_KEY_<n>`` variable in the environment or ``.env`` is collected
into ``api_key_pool``; ``api_keys(key_env)`` returns a provider's ordered,
deduplicated pool. ``app.llm.client`` rotates over it. Values never leave this
object except through ``api_keys``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.domain.negotiation import ACCEPT_LINE_PCT_OF_MAX_BP

# NAME or NAME_<n> where NAME ends in _KEY (GROQ_API_KEY, GROQ_API_KEY_2, ...).
_POOL_VAR_RE = re.compile(r"^[A-Z0-9_]*_KEY(?:_\d+)?$")


class _ApiKeyPoolSource(PydanticBaseSettingsSource):
    """Collect ``*_KEY`` / ``*_KEY_<n>`` vars from the dotenv and env sources.

    Wraps the already-built sources so ``Settings(_env_file=None)`` (tests) sees
    no ``.env`` keys. Env wins over ``.env``, like the declared fields.
    """

    def __init__(self, settings_cls: type[BaseSettings], *sources: Any) -> None:
        super().__init__(settings_cls)
        self._sources = sources

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        pool: dict[str, str] = {}
        for src in self._sources:
            for name, value in (getattr(src, "env_vars", None) or {}).items():
                upper = name.upper()
                if value and str(value).strip() and _POOL_VAR_RE.match(upper):
                    pool[upper] = str(value).strip()
        return {"api_key_pool": pool} if pool else {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Dict fields are deep-merged across sources, so an explicit
        # ``Settings(api_key_pool={})`` (hermetic tests) must drop the source.
        if "api_key_pool" in getattr(init_settings, "init_kwargs", {}):
            return (init_settings, env_settings, dotenv_settings, file_secret_settings)
        pool = _ApiKeyPoolSource(settings_cls, dotenv_settings, env_settings)
        return (init_settings, pool, env_settings, dotenv_settings, file_secret_settings)

    # LLM / STT keys (provider skipped when unset). SecretStr keeps them out of
    # repr() and tracebacks; read them through api_keys().
    groq_api_key: SecretStr | None = None
    mistral_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    cerebras_api_key: SecretStr | None = None
    # Paid Anthropic key: the eval ``judge`` role and
    # the demo ``nlu`` route's first target (under the daily budget below).
    anthropic_api_key: SecretStr | None = None
    # Daily cap (USD, UTC day) on route targets marked ``budgeted`` in
    # providers.yaml (only the demo NLU's Claude target). Decimal, never float;
    # app.llm.budget converts it to integer micro-dollars. Prices live in providers.yaml.
    claude_daily_budget_usd: Decimal = Field(default=Decimal("1.00"), ge=0)
    # Every NAME / NAME_<n> ending in _KEY from env + .env (see module docstring).
    # Read through api_keys(); SecretStr keeps values out of repr().
    api_key_pool: dict[str, SecretStr] = {}
    # Cooldown for a rate-limited key when the 429 carries no Retry-After (s).
    llm_key_cooldown_s: float = 60.0

    llm_profile: str = "demo"
    llm_cache: bool = True
    llm_cache_path: str = "llm_cache.db"
    # llm | bank | template. bank = guard-checked templates from nlg_bank_path, no LLM.
    nlg_mode: str = "llm"
    nlg_bank_path: str = "config/nlg_bank.json"
    # H3 conversational NLG: ack / answer acts before the decided move.
    nlg_h3: bool = False
    # Code-built acknowledgements: "Got it, up to 5 payments." before
    # the move, from ack_facts + ACK_VARIANTS only (no LLM, no answer act, no
    # NLG context). Independent of ``nlg_h3``; when both are on, H3 renders.
    nlg_ack: bool = True
    nlu_mode: str = "llm"
    # NLU output schema (Phase 71a): ``flat`` = one object of flags (shipped);
    # ``acts`` = typed acts + call context (``app.agent.nlu.acts``), converted
    # to the same ``VerifiedAnalysis`` so the policy is unchanged.
    nlu_schema: Literal["flat", "acts"] = "flat"
    # Per-request LLM timeouts (s). A timeout fails over to the next route target.
    llm_timeout_nlu_s: float = 6.0
    llm_timeout_nlg_s: float = 4.0
    # Sim phrasing is eval-only and not on the voice path; looser bound.
    llm_timeout_sim_s: float = 15.0
    # Eval-only A/B agent arms (eval/agents): multi-step tool calls, not on the voice path.
    llm_timeout_agent_s: float = 20.0
    # Eval-only naturalness judge (Anthropic): two transcripts in, one verdict out.
    llm_timeout_judge_s: float = 60.0

    db_path: str = "debt_settlement_agent.db"

    hostility_threshold: float = 0.8
    max_turns: int = 24
    anchor_ratio: float = 0.7
    concession_factor: float = 0.5
    max_counters: int = 6
    # Accept only at or below this share of the client's ceiling
    # (bp of max_bp; 7500 = 75.00%), and never counter above it. Integer math.
    accept_line_pct_of_max_bp: int = Field(default=ACCEPT_LINE_PCT_OF_MAX_BP, ge=0, le=10000)
    # Loop guard: hand off instead of asking the same question this
    # many + 1 times, or after this many rep turns in a row with no progress.
    max_same_question: int = 2
    max_no_progress_turns: int = 4

    # Which plan the engine picks among the legal ones (docs/ENGINE.md, Phase 59):
    # fee_first = the take-home ranking (earliest program fee, then fewest payments;
    # default, user decision 2026-10-27: client_first cost clients a median $19 more
    # in bank fees); client_first = most cushion, then smallest largest payment. The
    # firm's soft cushion (firm.json min_cushion_cents) applies under both. The fee
    # policy changes fee timing only, never which prices are affordable.
    engine_objective: Literal["fee_first", "client_first"] = "fee_first"
    engine_fee_policy: Literal["front_loaded", "proportional"] = "front_loaded"

    # --- Voice (Phase 63; app.voice.pipeline) ---
    # Deepgram key for the Pipecat path only (server-side; never sent to the browser).
    deepgram_api_key: SecretStr | None = None
    # Daily cap (USD, UTC day) on Deepgram voice spend; new voice sessions are
    # refused once today's estimate reaches it. Decimal, never float.
    voice_daily_budget_usd: Decimal = Field(default=Decimal("1.00"), ge=0)
    # Hosted-demo caps (Phase 66): Flux minutes per UTC day (new sessions refused
    # once reached) and voice seconds per call (the session ends with a message).
    voice_daily_minutes: Decimal = Field(default=Decimal("60"), ge=0)
    voice_session_max_s: int = Field(default=600, ge=10)
    # Cloudflare TURN (Phase 66; app.voice.pipeline.ice). Without both, voice
    # uses STUN only, which cannot connect from behind Render's HTTP ingress.
    cloudflare_turn_key_id: str | None = None
    cloudflare_turn_api_token: SecretStr | None = None
    voice_turn_ttl_s: int = Field(default=3600, ge=300, le=86400)
    # TURN for the server's own (aiortc) side. Keep on behind HTTP-only ingress
    # (Render); turn off on a host with a public IP (the AWS demo): the browser
    # still relays through TURN when its network needs it, and aioice's channel
    # bind against Cloudflare fails with 401 there anyway.
    voice_server_turn: bool = True
    # Deepgram list prices used for the estimate (verify before relying on them).
    deepgram_flux_usd_per_min: Decimal = Decimal("0.0077")
    deepgram_aura2_usd_per_1k_chars: Decimal = Decimal("0.030")
    voice_tts_voice: str = "aura-2-thalia-en"
    # Flux turn settings; None keeps Deepgram's defaults (eot 0.7, timeout 5000 ms).
    # One-word turns ("Correct.") need no change here: they were lost to gaps in
    # the audio stream, which app.voice.pipeline.processors.SilenceFill closes.
    voice_eot_threshold: float | None = Field(default=None, ge=0.5, le=0.9)
    voice_eot_timeout_ms: int | None = Field(default=None, ge=500, le=10000)
    voice_eager_eot_threshold: float | None = Field(default=0.4, ge=0.3, le=0.9)
    # Start NLU on Flux's EagerEndOfTurn; the committed turn reuses it or redoes it.
    voice_eager_nlu: bool = True
    # Barge step 1: local VAD pauses the agent when the rep talks over it (Flux's
    # words then stop or resume it). Off: words alone decide, about 1.4 s late.
    voice_vad_pause: bool = True
    # Pause-only VAD (not Flux), tuned down after the user's voice test: noise and
    # speaker echo paused the agent too often. Speech must score at least
    # ``confidence`` (Silero), be at least ``min_volume`` loud (Pipecat's 0-1
    # smoothed level; Pipecat default 0.6) and last ``start_s`` before a pause.
    voice_vad_confidence: float = Field(default=0.75, ge=0.3, le=0.99)
    voice_vad_min_volume: float = Field(default=0.7, ge=0.0, le=1.0)
    voice_vad_start_s: float = Field(default=0.3, ge=0.05, le=1.0)
    # A reply not out this long after the rep's final gets a short filler first
    # ("One moment."); None turns fillers off. See processors.FILLER_AFTER_S.
    voice_filler_after_ms: int | None = Field(default=3000, ge=300, le=10000)

    firm_name: str = "Synthetic Debt Relief"
    opening_disclosure: str = (
        "I am authorized to discuss settlement options for this account."
    )

    def api_keys(self, key_env: str) -> list[tuple[int, str]]:
        """Ordered, deduplicated ``(suffix, value)`` pool for ``key_env``.

        Suffix 0 is the unsuffixed ``key_env`` (the declared field when there is
        one, so ``Settings(groq_api_key=None)`` still disables it), then
        ``key_env_1``, ``key_env_2``, ... contiguous from 1. Empty values are skipped.
        """
        out: list[tuple[int, str]] = []
        seen: set[str] = set()

        def add(suffix: int, value: Any) -> None:
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if value is None or not str(value).strip():
                return
            v = str(value).strip()
            if v not in seen:
                seen.add(v)
                out.append((suffix, v))

        field = key_env.lower()
        if field in type(self).model_fields:
            add(0, getattr(self, field))
        else:
            add(0, self.api_key_pool.get(key_env))
        n = 1
        while f"{key_env}_{n}" in self.api_key_pool:
            add(n, self.api_key_pool[f"{key_env}_{n}"])
            n += 1
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide settings (env read once)."""
    return Settings()
