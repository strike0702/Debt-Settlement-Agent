"""TurnAnalysis / ExtractedTerm, and the NLU v2 act models — shared by NLU, policy, sim.

The simulator emits ground-truth ``TurnAnalysis`` for oracle NLU and must not
import ``app.agent``. Agent NLU re-exports these from ``app.agent.nlu_types``.

NLU v2 (Phase 71a, ``NLU_SCHEMA=acts``): the model returns ``ActList``, a
short list of typed acts from a closed set (``Act``, discriminated on
``type``), each with only its own fields and a verbatim ``quote``. Validation
is strict (unknown type or field fails); ``app.agent.nlu.acts`` turns a
failure into one ``unclear`` act and converts verified acts back into a
``TurnAnalysis`` so the policy runs unchanged. No checks against the
utterance live here.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Off-script question topics. Anything else the rep asks is "other".
QuestionTopic = Literal["why_not_higher", "next_steps", "who_approves", "timeline", "other"]
QUESTION_TOPICS: tuple[str, ...] = (
    "why_not_higher",
    "next_steps",
    "who_approves",
    "timeline",
    "other",
)


RuleField = Literal[
    "max_payments",
    "min_payment_cents",
    "payment_structure",
    "first_payment_date",
    "max_segments",
    "max_token_pays",
    "min_payment_tiers",
]


class ExtractedTerm(BaseModel):
    field: RuleField
    value: int | str | date | dict | list
    quote: str  # verbatim span from the utterance
    hedged: bool = False


class TurnAnalysis(BaseModel):
    terms: list[ExtractedTerm] = Field(default_factory=list)
    settlement_ask_pct: float | None = None  # percent points, e.g. 45.0 = 45%
    ask_quote: str | None = None
    # Settlement ask stated as a dollar total, integer cents. Code,
    # never the LLM, converts it to basis points of the account balance.
    settlement_ask_total_cents: int | None = None
    ask_total_quote: str | None = None
    # A dollar amount that may be the total to settle or the minimum per
    # payment. The agent asks which one instead of guessing.
    amount_ambiguous_cents: int | None = None
    amount_ambiguous_quote: str | None = None
    stance: Literal[
        "offer", "counter", "accept", "reject", "stall", "info", "question", "other"
    ] = "other"
    readback_response: Literal["confirm", "deny"] | None = None
    asks_client_private_info: bool = False
    demands_commitment: bool = False
    hostility: float = 0.0
    wants_to_end: bool = False
    asks_for_schedule: bool = False
    # True when the rep says the number is final / their floor / cannot go lower.
    firm: bool = False
    # Tiers phrased as "first N payments": dropped by NLU, policy asks to rephrase.
    tiers_ambiguous: bool = False
    # Off-script process question (not a term, price, schedule or private ask).
    # NLU-sourced only; ``decide()`` ignores it, ``app.agent.acts`` answers it.
    asks_question: bool = False
    question_topic: QuestionTopic | None = None
    # The rep announced a rule without giving it yet ("before that I need to
    # define the payment tiers", "there are three tiers"): the field, and how
    # many tiers / levels they said there will be. Code-checked in the NLU.
    defines_field: RuleField | None = None
    expected_count: int | None = None
    # "That's all" / "no more": the rep finished a list (tiers given one by one).
    list_done: bool = False
    # Numbers the rep said that no verified act claimed (raw spans, acts schema
    # only). The policy never acts on an older value on such a turn.
    unaccounted_numbers: list[str] = Field(default_factory=list)
    # The acts model could not map the line (``unclear`` act): ask to rephrase.
    unclear: bool = False


# --- NLU v2 acts ------------------------------------------------------------

ACT_TYPES: tuple[str, ...] = (
    "answer",
    "state_rule",
    "announce_rules",
    "rule_item",
    "list_done",
    "price_ask",
    "respond_to_offer",
    "readback_reply",
    "question",
    "ask_private_info",
    "demand_commitment",
    "hostility",
    "end_call",
    "stall",
    "backchannel",
    "unclear",
)


class _ActBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)
    # Verbatim span of the rep's line this act comes from.
    quote: str = Field(min_length=1, max_length=300)


class TermAnswerAct(_ActBase):
    """The rep answers the rule the agent just asked about."""

    type: Literal["answer"]
    field: RuleField
    value: int | str | list[dict[str, int]]
    hedged: bool = False


class StateRuleAct(_ActBase):
    """The rep volunteers a rule nobody asked about."""

    type: Literal["state_rule"]
    field: RuleField
    value: int | str | list[dict[str, int]]
    hedged: bool = False


class AnnounceRulesAct(_ActBase):
    """The rep says they will give a rule (optionally how many items) but not yet."""

    type: Literal["announce_rules"]
    field: RuleField
    count: int | None = Field(default=None, ge=1, le=10)


class RuleItemAct(_ActBase):
    """One item of a list being given (a payment tier); code re-reads the quote."""

    type: Literal["rule_item"]
    list: Literal["tiers"] = "tiers"
    from_payment: int | None = Field(default=None, ge=1, le=120)
    to_payment: int | None = Field(default=None, ge=1, le=120)
    min_cents: int | None = Field(default=None, gt=0)


class ListDoneAct(_ActBase):
    """The rep finished the list ("that's all the tiers")."""

    type: Literal["list_done"]
    list: Literal["tiers"] = "tiers"


class PriceAskAct(_ActBase):
    """A settlement price: percent of the balance or a dollar total, with firmness."""

    type: Literal["price_ask"]
    pct: float | None = Field(default=None, gt=0, le=100)
    total_cents: int | None = Field(default=None, gt=0)
    firmness: Literal["opening", "counter", "final"] = "counter"

    @model_validator(mode="after")
    def _one_amount(self) -> PriceAskAct:
        if (self.pct is None) == (self.total_cents is None):
            raise ValueError("price_ask needs exactly one of pct / total_cents")
        return self


class RespondToOfferAct(_ActBase):
    """Accept or reject the offer or schedule on the table."""

    type: Literal["respond_to_offer"]
    response: Literal["accept", "reject"]


class ReadbackReplyAct(_ActBase):
    """Yes / no to a pending read-back, with an optional corrected value."""

    type: Literal["readback_reply"]
    response: Literal["confirm", "deny"]
    correction: int | str | None = None


class QuestionAct(_ActBase):
    """An off-script process question."""

    type: Literal["question"]
    topic: QuestionTopic = "other"


class AskPrivateInfoAct(_ActBase):
    """The rep asks about the client's own money."""

    type: Literal["ask_private_info"]


class DemandCommitmentAct(_ActBase):
    """The rep demands a commitment the agent cannot give."""

    type: Literal["demand_commitment"]


class HostilityAct(_ActBase):
    """Insults or threats; ``strong`` escalates."""

    type: Literal["hostility"]
    level: Literal["mild", "strong"] = "mild"


class EndCallAct(_ActBase):
    """The rep closes, refuses to continue, or transfers the call."""

    type: Literal["end_call"]
    kind: Literal["goodbye", "refuse", "transfer"] = "goodbye"


class StallAct(_ActBase):
    """The rep puts off their own answer."""

    type: Literal["stall"]


class BackchannelAct(_ActBase):
    """Filler with no content ("mm-hmm", "okay")."""

    type: Literal["backchannel"]


class UnclearAct(BaseModel):
    """The line maps to no act; the agent asks the rep to rephrase."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["unclear"]
    quote: str = ""


Act = Annotated[
    TermAnswerAct
    | StateRuleAct
    | AnnounceRulesAct
    | RuleItemAct
    | ListDoneAct
    | PriceAskAct
    | RespondToOfferAct
    | ReadbackReplyAct
    | QuestionAct
    | AskPrivateInfoAct
    | DemandCommitmentAct
    | HostilityAct
    | EndCallAct
    | StallAct
    | BackchannelAct
    | UnclearAct,
    Field(discriminator="type"),
]


class ActList(BaseModel):
    """The acts model's whole reply: ``{"acts": [...]}`` (at most 8)."""

    model_config = ConfigDict(extra="forbid")
    acts: list[Act] = Field(default_factory=list, max_length=8)
