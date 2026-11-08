"""NLU acts: the closed act set and ``RepTurn``, shared by NLU, policy and sim.

The NLU model returns ``ActList``: a short list of typed acts from a closed
set (``Act``, discriminated on ``type``), each with only its own fields and a
verbatim ``quote``. Validation is strict (unknown type or field fails).
``RepTurn`` is one rep line as the policy reads it: the verified acts, with
code-checked values in the term and price acts, plus two code notes
(numbers no act claimed, tiers that cannot be placed). The simulator emits a
ground-truth ``RepTurn`` for oracle NLU (it must not import ``app.agent``);
``app.agent.nlu`` verifies model and oracle acts alike. No checks against
the utterance live here, and no decisions (``app.agent.policy`` dispatches).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Off-script question topics. Anything else the rep asks is "other".
# Off-script question topics; ``schedule`` (payment dates / schedule detail)
# is set by code and answered by the policy, the rest by ``app.agent.acts``.
QuestionTopic = Literal[
    "why_not_higher", "next_steps", "who_approves", "timeline", "schedule", "other"
]
QUESTION_TOPICS: tuple[str, ...] = (
    "why_not_higher",
    "next_steps",
    "who_approves",
    "timeline",
    "schedule",
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


# --- acts -----------------------------------------------------------------

# A rule's value: the model sends JSON (int, str, tier list); after code
# verification a date is a ``date`` and tiers are ``[(from_payment, min_cents)]``.
RuleValue = int | str | date | list[Any]

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
    "repeat_request",
    "unclear",
)

# Acts that may share a line with a repeat request without giving it content.
_REPEAT_COMPANIONS = frozenset({"repeat_request", "backchannel", "stall", "unclear"})


class _ActBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)
    # Verbatim span of the rep's line this act comes from.
    quote: str = Field(min_length=1, max_length=300)


class TermAnswerAct(_ActBase):
    """The rep answers the rule the agent just asked about."""

    type: Literal["answer"]
    field: RuleField
    value: RuleValue
    hedged: bool = False


class StateRuleAct(_ActBase):
    """The rep volunteers a rule nobody asked about."""

    type: Literal["state_rule"]
    field: RuleField
    value: RuleValue
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
    """A settlement price: percent of the balance or a dollar total, with firmness.

    Exactly one of ``pct`` / ``total_cents``. The one exception is a ``final``
    with neither: "that's the lowest I can go" restates the rep's standing ask
    as final (code sets it from a firm cue; the policy never reuses that ask
    while the line holds a number no act claimed).
    """

    type: Literal["price_ask"]
    pct: float | None = Field(default=None, gt=0, le=100)
    total_cents: int | None = Field(default=None, gt=0)
    firmness: Literal["opening", "counter", "final"] = "counter"

    @model_validator(mode="after")
    def _one_amount(self) -> PriceAskAct:
        both = self.pct is not None and self.total_cents is not None
        neither = self.pct is None and self.total_cents is None
        if both or (neither and self.firmness != "final"):
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


# ``hostility`` level → the 0..1 score the policy compares with ``hostility_threshold``.
HOSTILITY_LEVEL: dict[str, float] = {"mild": 0.4, "strong": 1.0}


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


class RepeatRequestAct(_ActBase):
    """The rep asks the agent to say again or recall what it said."""

    type: Literal["repeat_request"]


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
    | RepeatRequestAct
    | UnclearAct,
    Field(discriminator="type"),
]


class ActList(BaseModel):
    """The acts model's whole reply: ``{"acts": [...]}`` (at most 8)."""

    model_config = ConfigDict(extra="forbid")
    acts: list[Act] = Field(default_factory=list, max_length=8)


_TIERS = "min_payment_tiers"
_TERM_ACTS = (TermAnswerAct, StateRuleAct)


class RepTurn(BaseModel):
    """One rep line as the policy reads it: verified acts plus two code notes.

    ``acts`` keep the rep's order. Term acts carry code-verified values (dates
    as ``date``, tiers as ``[(from_payment, min_cents)]``); a ``price_ask``
    carries the verified percent (a dollar total is already converted). The
    readers below are views over ``acts``, not separate flags.
    """

    acts: list[Act] = Field(default_factory=list)
    # Numbers in the line that no verified act claimed (raw spans): the policy
    # will not act on an older value this turn.
    unaccounted_numbers: list[str] = Field(default_factory=list)
    # Tiers that cannot be placed ("the first N payments" with no range, or
    # ranges with a gap / overlap): the policy asks for them again.
    tiers_ambiguous: bool = False
    # Oracle ground truth only: a dollar amount whose meaning (total or per
    # payment) is unclear. The orchestrator asks about it before the policy
    # runs; for model acts code derives it (``app.agent.nlu.acts``).
    amount_ambiguous_cents: int | None = None
    amount_ambiguous_quote: str | None = None

    def of(self, kind: type[Any]) -> list[Any]:
        """Acts of one model class, in line order."""
        return [a for a in self.acts if isinstance(a, kind)]

    def has(self, type_name: str) -> bool:
        return any(a.type == type_name for a in self.acts)

    @property
    def price(self) -> PriceAskAct | None:
        """The last price act with a number (a self-correction keeps the later one)."""
        priced = [a for a in self.of(PriceAskAct) if a.pct is not None]
        return priced[-1] if priced else None

    @property
    def ask_pct(self) -> float | None:
        p = self.price
        return p.pct if p is not None else None

    @property
    def firm(self) -> bool:
        return any(a.firmness == "final" for a in self.of(PriceAskAct))

    @property
    def accepts(self) -> bool:
        return any(a.response == "accept" for a in self.of(RespondToOfferAct))

    @property
    def rejects(self) -> bool:
        return not self.accepts and any(
            a.response == "reject" for a in self.of(RespondToOfferAct)
        )

    @property
    def readback(self) -> str | None:
        """``confirm`` / ``deny`` from the last read-back reply, else None."""
        rb = self.of(ReadbackReplyAct)
        return rb[-1].response if rb else None

    @property
    def terms(self) -> list[TermAnswerAct | StateRuleAct]:
        return [a for a in self.acts if isinstance(a, _TERM_ACTS)]

    @property
    def announced(self) -> AnnounceRulesAct | None:
        ann = self.of(AnnounceRulesAct)
        return ann[0] if ann else None

    @property
    def list_done(self) -> bool:
        return self.has("list_done")

    @property
    def ends(self) -> bool:
        return self.has("end_call")

    def hostility(self) -> float:
        """Highest hostility score in the line (0 when none)."""
        return max((HOSTILITY_LEVEL[a.level] for a in self.of(HostilityAct)), default=0.0)

    @property
    def unclear(self) -> bool:
        return self.has("unclear")

    @property
    def repeat_only(self) -> bool:
        """A repeat request with no content or safety act beside it (mild hostility allowed)."""
        return self.has("repeat_request") and all(
            a.type in _REPEAT_COMPANIONS or (a.type == "hostility" and a.level == "mild")
            for a in self.acts
        )

    def asks(self, topic: str) -> bool:
        """True when a ``question`` act of ``topic`` is present."""
        return any(a.topic == topic for a in self.of(QuestionAct))

    def tier_items(self) -> list[tuple[int, int | None, int]]:
        """Tiers stated this turn: ``(from_payment, to_payment | None, min_cents)``.

        From ``rule_item`` acts (code-read, with their end) and from a tiers
        ``answer`` / ``state_rule`` (a whole set, no ends).
        """
        out: list[tuple[int, int | None, int]] = []
        for a in self.acts:
            if isinstance(a, RuleItemAct) and a.from_payment and a.min_cents:
                out.append((a.from_payment, a.to_payment, a.min_cents))
            elif isinstance(a, _TERM_ACTS) and a.field == _TIERS and isinstance(a.value, list):
                out.extend((int(x[0]), None, int(x[1])) for x in a.value)
        return out
