"""NLU result models: what the rest of the agent reads after a rep turn.

``RawReading`` is the content of a turn before verification (terms and price
read off the acts); ``VerifiedTerm`` / ``VerifiedAnalysis`` are its verified
form plus the verified non-content acts (``acts``: accept / reject, read-back
reply, question, closing, ...). ``to_rep_turn`` builds the policy's input
(``app.domain.nlu_types.RepTurn``). ``DroppedTerm`` records what verification
threw away, for the decision trace only (policy never reads it). No logic
beyond the view helpers; built by ``acts.verify_acts``, ``fast_paths`` and
``amounts``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.domain.nlu_types import (
    Act,
    PriceAskAct,
    RepTurn,
    RuleItemAct,
    StateRuleAct,
)


class RawTerm(BaseModel):
    """A rule value as an act stated it, before the quote / number checks."""

    field: str
    value: int | str | date | dict | list
    quote: str
    hedged: bool = False


class RawReading(BaseModel):
    """Turn content read off the acts, before verification (``verify.post_verify``)."""

    terms: list[RawTerm] = Field(default_factory=list)
    settlement_ask_pct: float | None = None  # percent points, e.g. 45.0 = 45%
    ask_quote: str | None = None
    settlement_ask_total_cents: int | None = None
    ask_total_quote: str | None = None
    # Oracle only (``RepTurn.amount_ambiguous_*``): ground truth that an amount's
    # meaning is unclear. Model acts have no such act; code derives it instead.
    amount_ambiguous_cents: int | None = None
    amount_ambiguous_quote: str | None = None


class VerifiedTerm(BaseModel):
    """Extracted term after quote / number / prior-range checks."""

    field: str
    value: Any
    quote: str
    hedged: bool = False
    verified: bool


class DroppedTerm(BaseModel):
    """A term (or the ask) ``post_verify`` rejected; ``reason`` = audit event minus ``nlu_``."""

    field: str
    value: Any = None
    reason: str
    quote: str | None = None


class VerifiedAnalysis(BaseModel):
    """One rep turn after code verification: checked content plus verified acts.

    Content slots (terms, the ask, dollar amounts) hold code-verified numbers;
    ``acts`` hold the turn's other verified acts (``respond_to_offer``,
    ``readback_reply``, ``question``, ``ask_private_info``,
    ``demand_commitment``, ``hostility``, ``end_call``, ``stall``,
    ``backchannel``, ``announce_rules``, ``list_done``, ``unclear``, and a
    number-free ``final`` price act). ``to_rep_turn`` is the policy's view.
    """

    terms: list[VerifiedTerm] = Field(default_factory=list)
    # Decision-trace only (never read by policy): what verification threw away.
    dropped: list[DroppedTerm] = Field(default_factory=list)
    settlement_ask_pct: float | None = None
    ask_quote: str | None = None
    ask_verified: bool = False
    # Dollar-total ask: verified against its quote here; converted to
    # ``settlement_ask_pct`` by ``resolve_amounts`` once the balance is known.
    settlement_ask_total_cents: int | None = None
    ask_total_quote: str | None = None
    # Set by ``resolve_amounts`` when the ask came from a dollar total.
    ask_total_bp: int | None = None
    # Verified dollar amount whose meaning (total vs per payment) is unclear.
    amount_ambiguous_cents: int | None = None
    amount_ambiguous_quote: str | None = None
    # Bare number for a cents field with no $/dollars/cents cue — ask which unit.
    cents_ambiguity_bare: int | None = None
    cents_ambiguity_field: str | None = None
    tiers_ambiguous: bool = False
    # Tiers given as ``rule_item`` acts: ``(from, to | None, min_cents)`` each.
    tier_items: list[tuple[int, int | None, int]] = Field(default_factory=list)
    # Verified non-content acts (see the class docstring), in line order.
    acts: list[Act] = Field(default_factory=list)
    # Numbers no verified act claimed; the policy will not act on older values.
    unaccounted_numbers: list[str] = Field(default_factory=list)
    # Orchestrator only: the line cues a post-proposal change of a rule.
    revises_terms: bool = False
    # Trace and corpus label only (offer / counter / accept / ...): the policy
    # and orchestrator read ``acts``, never this.
    stance: str = "other"
    # Trace only: the act types the model returned and kept.
    model_acts: list[str] = Field(default_factory=list)

    def has(self, type_name: str) -> bool:
        return any(a.type == type_name for a in self.acts)

    def first(self, type_name: str) -> Any | None:
        return next((a for a in self.acts if a.type == type_name), None)

    @property
    def accepts(self) -> bool:
        return any(a.type == "respond_to_offer" and a.response == "accept" for a in self.acts)

    @property
    def rejects(self) -> bool:
        return not self.accepts and any(
            a.type == "respond_to_offer" and a.response == "reject" for a in self.acts
        )

    @property
    def readback_response(self) -> str | None:
        rb = [a for a in self.acts if a.type == "readback_reply"]
        return rb[-1].response if rb else None

    @property
    def interrupts(self) -> bool:
        """A closing, private ask, lock-in demand or hostile line (outranks a held question)."""
        return any(
            a.type in ("end_call", "ask_private_info", "demand_commitment")
            or (a.type == "hostility" and a.level == "strong")
            for a in self.acts
        )

    def to_rep_turn(self) -> RepTurn:
        """The policy's input: verified rules and price as acts, then the other acts."""
        content: list[Any] = []
        items = {(frm, cents): to for frm, to, cents in self.tier_items}
        for t in self.terms:
            if t.field == "min_payment_tiers" and items and isinstance(t.value, list):
                for frm, cents in t.value:
                    content.append(
                        RuleItemAct(
                            type="rule_item",
                            quote=(t.quote or "tiers")[:300],
                            from_payment=frm,
                            to_payment=items.get((frm, cents)),
                            min_cents=cents,
                        )
                    )
                continue
            content.append(
                StateRuleAct.model_construct(
                    type="state_rule",
                    field=t.field,
                    value=t.value,
                    quote=(t.quote or t.field)[:300],
                    hedged=t.hedged,
                )
            )
        rest = list(self.acts)
        if self.settlement_ask_pct is not None:
            final = any(isinstance(a, PriceAskAct) and a.firmness == "final" for a in rest)
            rest = [a for a in rest if not isinstance(a, PriceAskAct)]
            content.append(
                PriceAskAct(
                    type="price_ask",
                    quote=(self.ask_quote or "price")[:300],
                    pct=self.settlement_ask_pct,
                    firmness="final" if final else "counter",
                )
            )
        return RepTurn(
            acts=[*content, *rest],
            unaccounted_numbers=list(self.unaccounted_numbers),
            tiers_ambiguous=self.tiers_ambiguous,
        )
