"""Code-based creditor policy + phraser for offline/online sims.

``CreditorPolicy`` decides moves from the agent's ``Action`` intent and PUBLIC
facts plus spoken text — never PRIVATE client numbers. Accept/reject and rule
reveals are deterministic; the phraser (``sim`` LLM role or templates) only
styles the line and emits the ground-truth acts (``RepTurn``) for oracle NLU.

Reveal schedule (all 7 ``TrueRules`` fields, like a real rep):
- opening: the core four (max payments, minimum, structure, first payment date)
- ``ASK``: the asked field; the optional sweep ("any tiers or level limit?"):
  tiers and payment levels not yet spoken
- ``defines_tiers`` rep: at the sweep or settlement ask it announces its
  tiers, then says how many, then gives one tier per guided ask
- ``READ_BACK`` / schedule read-back (``CONFIRM_SCHEDULE``/``SPEAK_SCHEDULE``):
  any still-unspoken late field (payment levels, token payments, tiers)

``COUNTER_TERMS`` is judged against the hidden rules as hard limits: a later
first payment date, lower minimum, or more payments than the truth is
rejected; anything inside the truth is accepted and becomes an agreed term
(``agreed_rules``) that schedule checks and eval scoring use. Hidden limits
never loosen, so scenario labels (zopa / stratum) stay valid.

Price counters follow the scenario's haggle style (``sim.haggle``: easy,
holder, stepper, staller). Every style accepts only at or above the floor.

Lines: each template has a few plain variants (``_LINES``), picked
by ``Random(f"lines:{scenario.id}")`` so a scenario always speaks the same
way. Figures in every variant come from ``app.domain.units`` renderers.

LLM phrasing (``phrasing="llm"``): the rewrite is kept only when it carries
exactly the draft's figures, adds no client-private wording, commitment
language or opposite stance (``sim.figures.rewrite_problem``); otherwise the
draft is spoken and the fallback is counted by reason (``rewrite_stats``).
The call allows ``SIM_MAX_TOKENS``: reasoning models (gpt-oss) spend
part of the budget thinking, and at 120 a reply could come back empty.

Must not import ``app.agent``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import date
from random import Random
from string import ascii_lowercase
from typing import Any, Literal, Protocol

from app.adapter.engine_adapter import evaluate
from app.adapter.validator import validate
from app.domain.actions import Action, Intent
from app.domain.facts import Fact
from app.domain.nlu_types import (
    AnnounceRulesAct,
    AskPrivateInfoAct,
    DemandCommitmentAct,
    EndCallAct,
    PriceAskAct,
    ReadbackReplyAct,
    RepTurn,
    RespondToOfferAct,
    StallAct,
    StateRuleAct,
)
from app.domain.units import (
    render_count,
    render_date,
    render_money,
    render_ordinal,
    render_pct,
)
from app.llm.client import LLMUnavailable
from feasibility.models import CreditorRules
from sim.figures import FallbackReason, rewrite_problem
from sim.personas import Persona, get_persona
from sim.scenarios import Scenario, TrueRules, to_creditor_rules

PhrasingMode = Literal["template", "llm"]

LATE_FIELDS: tuple[str, ...] = ("max_segments", "max_token_pays", "min_payment_tiers")

# COUNTER_TERMS fact id → TrueRules field.
_ALT_FACT_FIELDS: dict[str, str] = {
    "alt_first_payment_date": "first_payment_date",
    "alt_min_payment_cents": "min_payment_cents",
    "alt_max_payments": "max_payments",
}

# Phrases the sim LLM must not inject (would false-trigger agent escalate).
_COMMIT_PHRASE_RE = re.compile(
    r"\b(?:"
    r"commit(?:ment|s|ted|ting)?"
    r"|lock(?:ed|ing)?\s+in"
    r"|guarantee(?:s|d)?"
    r"|it's a deal"
    r"|we have a deal"
    r")\b",
    re.IGNORECASE,
)


# Plain variants per line; ``{…}`` slots take rendered figures only.
_LINES: dict[str, tuple[str, ...]] = {
    "opening": (
        "We can take up to {max_p} payments, minimum {min_c}, {structure} payments, "
        "with the first payment due {fpd}.",
        "We can do up to {max_p} payments of at least {min_c} each, {structure} payments, "
        "with the first one due {fpd}.",
        "Up to {max_p} payments, {min_c} minimum, {structure} payments, "
        "and the first payment is due {fpd}.",
    ),
    "opening_flip": (
        "Actually, make that a maximum of {max_p} payments, minimum {min_c}, "
        "{structure} structure.",
        "Sorry, let me correct that: up to {max_p} payments, minimum {min_c}, "
        "{structure} structure.",
    ),
    "max_payments": (
        "The maximum is {q} payments.",
        "We can go up to {q} payments.",
        "Up to {q} payments.",
    ),
    "max_payments_flip": (
        "Actually the maximum is {q} payments.",
        "Sorry, correction: the maximum is {q} payments.",
    ),
    "min_payment": (
        "Our minimum payment is {q}.",
        "Each payment has to be at least {q}.",
        "The smallest payment we take is {q}.",
    ),
    "min_payment_flip": (
        "Actually our minimum payment is {q}.",
        "Sorry, the minimum payment is actually {q}.",
    ),
    "structure": ("We need {s} payments.", "The plan has to use {s} payments."),
    "first_payment_date": (
        "The first payment is due {q}.",
        "The first payment would be due {q}.",
        "First payment is due {q}.",
    ),
    "max_segments": (
        "We allow up to {q} payment levels.",
        "No more than {q} different payment amounts.",
        "There can be at most {q} payment levels.",
    ),
    "max_token_pays": (
        "Up to {q} of the payments can be token payments.",
        "We allow at most {q} token payments.",
        "No more than {q} token payments.",
    ),
    "no_tiers": (
        "There are no tiered minimums.",
        "The minimum stays the same for every payment.",
        "No tiers, the minimum is the same throughout.",
    ),
    "tier": (
        "From the {o} payment on, the minimum is {m}.",
        "Starting with the {o} payment, the minimum is {m}.",
    ),
    "sweep_none": ("No, nothing like that.", "No, none of those.", "No, nothing else."),
    "define_tiers": (
        "Before we get to a price, I need to define the payment tiers.",
        "Hold on, before that I need to define the payment tiers.",
    ),
    "tier_count": ("We will have {n} payment tiers.", "There are {n} payment tiers."),
    "tier_step": (
        "The {k} tier is from the {o} payment on, at least {m}.",
        "The {k} tier starts at the {o} payment, at least {m}.",
    ),
    "readback_yes": ("Yes, that's right.", "Correct.", "That's right.", "Yes, that's correct."),
    "readback_yes_value": ("Correct, {v}.", "Yes, {v}, that's right."),
    "readback_no": ("No, that's not right.", "No, that's not correct.", "No, that's wrong."),
    "ask": (
        "We are looking for a {p} settlement.",
        "We'd need {p} to settle this.",
        "We're looking for {p}.",
    ),
    "accept_counter": ("We can accept {p}.", "{p} works for us.", "OK, we can do {p}."),
    "concede": (
        "That is too low. We could come down to {p}.",
        "That's too low for us. We could do {p}.",
        "We can't do that, but we could come down to {p}.",
    ),
    "floor": (
        "That is too low. {p} is our floor; we cannot go lower.",
        "{p} is our floor. We can't go any lower.",
        "Sorry, {p} is the lowest we can go.",
    ),
    "hold": (
        "We're staying at {p}.",
        "That's too low. We're still at {p}.",
        "We can't move on that yet. Still {p}.",
    ),
    "stall_ask": (
        "I'd have to check with my supervisor before I give you a number.",
        "I don't have a figure for you yet.",
        "Let me look into that and get back to you on the number.",
    ),
    "stall_counter": (
        "Let me take that to my supervisor.",
        "I'll need to think about that.",
        "Hmm, let me check on that.",
    ),
    "schedule_ok": (
        "Yes, that payment schedule works for us. Agreed.",
        "That schedule works for us. Agreed.",
        "Yes, that works. Agreed.",
    ),
    "schedule_below_floor": (
        "The schedule shape is fine, but we cannot go below {p}.",
        "The payments look fine, but we can't go under {p}.",
    ),
    "terms_yes": (
        "Yes, we can accept that change.",
        "Yes, we can do that.",
        "OK, that change works.",
    ),
    "terms_no": (
        "No, that change does not work for us.",
        "No, we can't do that.",
        "Sorry, that change won't work for us.",
    ),
    "goodbye": ("Understood. Goodbye.", "OK, thanks. Goodbye.", "All right. Goodbye."),
}


_COUNT_WORD = {2: "two", 3: "three", 4: "four", 5: "five"}
_NTH_WORD = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}


def _spoken_tiers(action: Action, prefix: str) -> list[tuple[int, int]]:
    """Rebuild ``[(from_payment, min_cents)]`` from lettered tier facts on ``action``."""
    out: list[tuple[int, int]] = []
    for tag in ascii_lowercase:
        frm = action.facts.get(f"{prefix}_tier_{tag}_from")
        cents = action.facts.get(f"{prefix}_tier_{tag}_min")
        if frm is None or cents is None:
            break
        out.append((int(frm.value), int(cents.value)))  # type: ignore[arg-type]
    return out


# Completion budget for one rewrite. The prompt still asks for one or two short
# sentences; the headroom is for gpt-oss reasoning tokens, which count against
# max_tokens (at 120, 8 of 28 rewrites came back empty on Cerebras).
SIM_MAX_TOKENS = 512


class _SimLLM(Protocol):
    async def chat_text(
        self,
        role: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str: ...


def _acts(*acts: Any) -> RepTurn:
    """Ground truth for one line: its acts, in order (empty = a plain statement)."""
    return RepTurn(acts=list(acts))


def _respond(response: str, quote: str) -> RespondToOfferAct:
    return RespondToOfferAct(type="respond_to_offer", response=response, quote=quote)  # type: ignore[arg-type]


def _price(pct: float, quote: str, *, firm: bool = False) -> PriceAskAct:
    return PriceAskAct(
        type="price_ask", pct=pct, quote=quote, firmness="final" if firm else "counter"
    )


def _announce_tiers(quote: str, count: int | None = None) -> AnnounceRulesAct:
    return AnnounceRulesAct(
        type="announce_rules", field="min_payment_tiers", count=count, quote=quote
    )


def _stance_of(turn: RepTurn) -> str:
    """The draft's stance for the rewrite check: read-back answer, accept / reject, else info."""
    if turn.readback is not None:
        return turn.readback
    if turn.accepts:
        return "accept"
    if turn.rejects:
        return "reject"
    return "offer" if turn.price is not None else "info"


@dataclass
class CreditorReply:
    """Spoken creditor line plus its ground-truth acts (oracle NLU)."""

    text: str
    analysis: RepTurn


@dataclass
class CreditorPolicy:
    """Hidden-rule creditor that negotiates against agent Actions."""

    scenario: Scenario
    phrasing: PhrasingMode = "template"
    llm: _SimLLM | None = None
    persona: Persona = field(init=False)
    rules: CreditorRules = field(init=False)
    current_ask_bp: int = field(init=False)
    _turn: int = field(default=0, init=False)
    _revealed: set[str] = field(default_factory=set, init=False)
    _contradicted: bool = field(default=False, init=False)
    _spoken_max_payments: int | None = field(default=None, init=False)
    _spoken_min_payment: int | None = field(default=None, init=False)
    _done: bool = field(default=False, init=False)
    # ``defines_tiers`` rep: 0 nothing said, 1 announced, 2 count said; tiers given.
    _define_stage: int = field(default=0, init=False)
    _tiers_given: int = field(default=0, init=False)
    # Accepted COUNTER_TERMS overrides (TrueRules field → value).
    _agreed: dict[str, Any] = field(default_factory=dict, init=False)
    # Haggle state: counters still to answer with a hold, next step, firm yet.
    _holds_left: int = field(default=0, init=False)
    _step_i: int = field(default=0, init=False)
    _firm: bool = field(default=False, init=False)
    _lines: Random = field(init=False)
    # LLM rewrites tried, and drafts spoken instead by reason (eval reporting).
    rewrite_attempts: int = field(default=0, init=False)
    rewrite_fallbacks: Counter[FallbackReason] = field(default_factory=Counter, init=False)

    def __post_init__(self) -> None:
        self.persona = get_persona(self.scenario.persona)
        self.rules = to_creditor_rules(
            self.scenario.true_rules,
            program_fee_pct=self.scenario.call.program_fee_pct,
            bank_fee_cents=self.scenario.call.bank_fee_cents,
        )
        self.current_ask_bp = self.scenario.opening_ask_bp
        self._holds_left = self.scenario.haggle.hold_turns
        self._lines = Random(f"lines:{self.scenario.id}")

    @property
    def done(self) -> bool:
        return self._done

    @property
    def rewrite_stats(self) -> dict[str, Any]:
        """``{"attempts", "fallbacks": {reason: n}}`` for this call's LLM rewrites."""
        return {"attempts": self.rewrite_attempts, "fallbacks": dict(self.rewrite_fallbacks)}

    def _say(self, key: str, **slots: str) -> str:
        """One variant of line ``key`` (seeded per scenario), slots filled."""
        return self._lines.choice(_LINES[key]).format(**slots)

    @property
    def agreed_rules(self) -> TrueRules:
        """Hidden rules with accepted ``COUNTER_TERMS`` applied (eval ground truth)."""
        return dc_replace(self.scenario.true_rules, **self._agreed)

    def _public_facts(self, action: Action) -> dict[str, Fact]:
        return {
            k: v
            for k, v in action.facts.items()
            if v.visibility == "PUBLIC"
        }

    def _rule_value(self, field: str) -> Any:
        tr = self.agreed_rules
        return {
            "max_payments": tr.max_payments,
            "min_payment_cents": tr.min_payment_cents,
            "payment_structure": tr.payment_structure,
            "first_payment_date": tr.first_payment_date,
            "max_segments": tr.max_segments,
            "max_token_pays": tr.max_token_pays,
            "min_payment_tiers": list(tr.min_payment_tiers),
        }[field]

    def _term(
        self,
        field: str,
        value: Any,
        quote: str,
        *,
        hedged: bool = False,
    ) -> StateRuleAct:
        return StateRuleAct(
            type="state_rule", field=field, value=value, quote=quote, hedged=hedged  # type: ignore[arg-type]
        )

    def _maybe_pressure(self) -> CreditorReply | None:
        """Pressuring persona overrides on configured creditor turns."""
        if self._turn in self.persona.pressure_private_turns:
            commit = self._turn in self.persona.pressure_commit_turns
            if commit:
                text = (
                    "Before we continue, what is the client's monthly income, "
                    "and I need you to commit to this settlement today."
                )
                return CreditorReply(
                    text=text,
                    analysis=_acts(
                        AskPrivateInfoAct(type="ask_private_info", quote=text),
                        DemandCommitmentAct(type="demand_commitment", quote=text),
                    ),
                )
            text = "What is the client's monthly income and bank balance?"
            return CreditorReply(
                text=text,
                analysis=_acts(AskPrivateInfoAct(type="ask_private_info", quote=text)),
            )
        if self._turn in self.persona.pressure_commit_turns:
            text = "I need a firm commitment that this deal is locked in."
            return CreditorReply(
                text=text,
                analysis=_acts(DemandCommitmentAct(type="demand_commitment", quote=text)),
            )
        return None

    def _render_date(self, d: date) -> str:
        return render_date(d, self.scenario.call.client.as_of_date)

    def _reveal_structure_line(self) -> tuple[str, list[StateRuleAct]]:
        """Speak the core four rules (and track reveals / optional contradiction)."""
        tr = self.agreed_rules
        max_p = tr.max_payments
        min_c = tr.min_payment_cents
        structure = tr.payment_structure

        # Contradictory: after first reveal of max_payments, flip once later.
        if (
            self.persona.contradict_once
            and not self._contradicted
            and "max_payments" in self._revealed
            and self._spoken_max_payments is not None
        ):
            new_max = max_p + (2 if max_p < 20 else -2)
            if new_max < 1:
                new_max = max_p + 2
            self._contradicted = True
            self._spoken_max_payments = new_max
            quote_max = render_count(new_max)
            text = self._say(
                "opening_flip", max_p=quote_max, min_c=render_money(min_c), structure=structure
            )
            terms = [
                self._term("max_payments", new_max, quote_max),
                self._term("min_payment_cents", min_c, render_money(min_c)),
                self._term("payment_structure", structure, structure),
            ]
            return text, terms

        self._revealed.update(
            {"max_payments", "min_payment_cents", "payment_structure", "first_payment_date"}
        )
        self._spoken_max_payments = max_p
        self._spoken_min_payment = min_c
        fpd_q = self._render_date(tr.first_payment_date)
        text = self._say(
            "opening",
            max_p=render_count(max_p),
            min_c=render_money(min_c),
            structure=structure,
            fpd=fpd_q,
        )
        terms = [
            self._term("max_payments", max_p, render_count(max_p)),
            self._term("min_payment_cents", min_c, render_money(min_c)),
            self._term("payment_structure", structure, structure),
            self._term("first_payment_date", tr.first_payment_date, fpd_q),
        ]
        return text, terms

    def _reveal_late_fields(self) -> tuple[str, list[StateRuleAct]]:
        """Speak every late field not yet revealed ("" when none remain)."""
        texts: list[str] = []
        terms: list[StateRuleAct] = []
        for name in LATE_FIELDS:
            if name in self._revealed:
                continue
            t, ts = self._reveal_field(name)
            texts.append(t)
            terms.extend(ts)
        return " ".join(texts), terms

    def _with_late_fields(self, reply: CreditorReply) -> CreditorReply:
        """Prefix ``reply`` with any unspoken late fields (read-back moments)."""
        text, terms = self._reveal_late_fields()
        if not terms:
            return reply
        return CreditorReply(
            text=f"{text} {reply.text}",
            analysis=reply.analysis.model_copy(update={"acts": [*terms, *reply.analysis.acts]}),
        )

    def _reveal_field(self, field: str) -> tuple[str, list[StateRuleAct]]:
        value = self._rule_value(field)
        self._revealed.add(field)
        if field == "max_payments":
            # Contradictory flip if already revealed once.
            if (
                self.persona.contradict_once
                and not self._contradicted
                and self._spoken_max_payments is not None
            ):
                new_max = int(value) + 2
                self._contradicted = True
                self._spoken_max_payments = new_max
                q = render_count(new_max)
                return self._say("max_payments_flip", q=q), [
                    self._term(field, new_max, q)
                ]
            self._spoken_max_payments = int(value)
            q = render_count(int(value))
            return self._say("max_payments", q=q), [self._term(field, int(value), q)]
        if field == "min_payment_cents":
            if (
                self.persona.contradict_once
                and not self._contradicted
                and self._spoken_min_payment is not None
            ):
                new_min = int(value) + 5000
                self._contradicted = True
                self._spoken_min_payment = new_min
                q = render_money(new_min)
                return self._say("min_payment_flip", q=q), [
                    self._term(field, new_min, q)
                ]
            self._spoken_min_payment = int(value)
            q = render_money(int(value))
            return self._say("min_payment", q=q), [self._term(field, int(value), q)]
        if field == "payment_structure":
            s = str(value)
            return self._say("structure", s=s), [self._term(field, s, s)]
        if field == "first_payment_date":
            assert isinstance(value, date)
            q = self._render_date(value)
            return self._say("first_payment_date", q=q), [self._term(field, value, q)]
        if field == "max_segments":
            q = render_count(int(value))
            return self._say("max_segments", q=q), [self._term(field, int(value), q)]
        if field == "max_token_pays":
            q = render_count(int(value))
            return self._say("max_token_pays", q=q), [self._term(field, int(value), q)]
        if field == "min_payment_tiers":
            tiers = list(value)
            if not tiers:
                text = self._say("no_tiers")
                return text, [self._term(field, [], text.rstrip("."))]
            text = " ".join(
                self._say("tier", o=render_ordinal(frm), m=render_money(cents))
                for frm, cents in tiers
            )
            return text, [self._term(field, tiers, text.rstrip("."))]
        return "I am not sure about that term.", []

    def _define_tiers_reply(self) -> CreditorReply | None:
        """Next line of a rep who defines its tiers one by one (None once all are given).

        Announces the tiers, then the count, then one tier per ask, as the
        oracle NLU of a model that reads the rep correctly would.
        """
        tiers = list(self.agreed_rules.min_payment_tiers)
        if not self.scenario.defines_tiers or self._tiers_given >= len(tiers):
            return None
        if self._define_stage == 0:
            self._define_stage = 1
            text = self._say("define_tiers")
            return CreditorReply(text=text, analysis=_acts(_announce_tiers(text)))
        if self._define_stage == 1:
            self._define_stage = 2
            n = len(tiers)
            text = self._say("tier_count", n=_COUNT_WORD.get(n, render_count(n)))
            return CreditorReply(text=text, analysis=_acts(_announce_tiers(text, n)))
        frm, cents = tiers[self._tiers_given]
        self._tiers_given += 1
        self._revealed.add("min_payment_tiers")
        k = _NTH_WORD.get(self._tiers_given, "next")
        text = self._say("tier_step", k=k, o=render_ordinal(frm), m=render_money(cents))
        return CreditorReply(
            text=text,
            analysis=_acts(self._term("min_payment_tiers", [(frm, cents)], text.rstrip("."))),
        )

    def _sweep_reply(self) -> CreditorReply:
        """Answer "Any tiered minimums, or a limit on payment levels?"."""
        defining = self._define_tiers_reply()
        if defining is not None:
            return defining
        texts: list[str] = []
        terms: list[StateRuleAct] = []
        for name in ("min_payment_tiers", "max_segments"):
            if name not in self._revealed:
                t, ts = self._reveal_field(name)
                texts.append(t)
                terms.extend(ts)
        if not terms:
            return CreditorReply(text=self._say("sweep_none"), analysis=_acts())
        return CreditorReply(text=" ".join(texts), analysis=_acts(*terms))

    def _ask_settlement_reply(self) -> CreditorReply:
        if self.scenario.haggle.stall_on == "ask":
            # Staller: never names a number (the agent's repeat-question guard hands off).
            text = self._say("stall_ask")
            return CreditorReply(text=text, analysis=_acts(StallAct(type="stall", quote=text)))
        pct = self.current_ask_bp / 100.0
        spoken = render_pct(self.current_ask_bp)
        text = self._say("ask", p=spoken)
        return CreditorReply(text=text, analysis=_acts(_price(pct, spoken)))

    def _min_reject(self, lead: str) -> CreditorReply:
        """Reject a schedule and restate the (agreed) minimum payment."""
        min_c = self.agreed_rules.min_payment_cents
        floor_m = render_money(min_c)
        text = f"{lead} — our minimum is actually {floor_m}."
        return CreditorReply(
            text=text,
            analysis=_acts(
                _respond("reject", text), self._term("min_payment_cents", min_c, floor_m)
            ),
        )

    def _check_confirm(self, action: Action) -> CreditorReply:
        """Validate CONFIRM_SCHEDULE under agreed rules; reveal violations."""
        public = self._public_facts(action)
        bp_fact = public.get("settlement_pct")
        if bp_fact is None or not isinstance(bp_fact.value, int):
            text = "I need a clear settlement percentage on that schedule."
            return CreditorReply(text=text, analysis=_acts(_respond("reject", text)))
        bp = int(bp_fact.value)
        fpd = self.agreed_rules.first_payment_date
        summary = evaluate(self.scenario.call, self.rules, bp, fpd)
        if not summary.feasible or summary.rows is None:
            return self._min_reject("That schedule does not work for us")
        violations = validate(
            summary.rows,
            self.scenario.call.client,
            summary.offer_total_cents,
            summary.program_fee_cents,
            self.rules,
            fpd,
        )
        # Spoken offer_total / num_payments must match the agreed-rules eval.
        spoken_total = public.get("offer_total")
        spoken_n = public.get("num_payments")
        mismatch = False
        if spoken_total is not None and isinstance(spoken_total.value, int):
            if int(spoken_total.value) != summary.offer_total_cents:
                mismatch = True
        if spoken_n is not None and isinstance(spoken_n.value, int):
            pay_n = len([r for r in summary.rows if r.creditor_payment_cents > 0])
            if int(spoken_n.value) != pay_n:
                mismatch = True
        if violations or mismatch:
            return self._min_reject("Those payment amounts are off")
        if bp < self.scenario.floor_bp:
            text = self._say("schedule_below_floor", p=render_pct(self.scenario.floor_bp))
            self.current_ask_bp = max(
                self.scenario.floor_bp, self.current_ask_bp - 500
            )
            return CreditorReply(
                text=text,
                analysis=_acts(
                    _respond("reject", text),
                    _price(self.current_ask_bp / 100.0, render_pct(self.current_ask_bp)),
                ),
            )
        text = self._say("schedule_ok")
        return CreditorReply(
            text=text,
            analysis=_acts(
                _respond("accept", text),
                ReadbackReplyAct(type="readback_reply", response="confirm", quote=text),
            ),
        )

    def _readback_value_phrase(self, field: str, value: Any) -> str | None:
        """Rendered figure for a confirmed read-back ("6 payments", "$150"), else None."""
        if field == "max_payments" and isinstance(value, int):
            return f"{render_count(value)} payments"
        if field == "min_payment_cents" and isinstance(value, int):
            return render_money(value)
        return None

    def _price_reply(self, key: str, *, firm: bool = False) -> CreditorReply:
        """Reject our counter, stating the current ask (``key``: concede / hold / floor)."""
        spoken = render_pct(self.current_ask_bp)
        text = self._say(key, p=spoken)
        return CreditorReply(
            text=text,
            analysis=_acts(
                _respond("reject", text),
                _price(self.current_ask_bp / 100.0, spoken, firm=firm),
            ),
        )

    def _on_counter(self, action: Action) -> CreditorReply:
        public = self._public_facts(action)
        bp_fact = public.get("counter_pct")
        if bp_fact is None or not isinstance(bp_fact.value, int):
            # A question about our own move: no act (the agent restates it).
            return CreditorReply(
                text="Could you repeat that counter offer as a percentage?", analysis=_acts()
            )
        bp = int(bp_fact.value)
        floor = self.scenario.floor_bp
        haggle = self.scenario.haggle
        if haggle.stall_on == "counter":
            # Staller: no number, no stance, never accepts (loop guard territory).
            text = self._say("stall_counter")
            return CreditorReply(text=text, analysis=_acts(StallAct(type="stall", quote=text)))
        if bp >= floor:
            text = self._say("accept_counter", p=render_pct(bp))
            return CreditorReply(text=text, analysis=_acts(_respond("accept", text)))
        if self._firm:
            return self._price_reply("floor", firm=True)
        if self._holds_left > 0:
            # Holder: restate the number (not firm) before the next concession.
            self._holds_left -= 1
            return self._price_reply("hold")
        steps = haggle.steps_bp
        step = steps[self._step_i % len(steps)]
        self._step_i += 1
        self._holds_left = haggle.hold_turns
        self.current_ask_bp = max(floor, self.current_ask_bp - step)
        if self.current_ask_bp <= floor:
            self._firm = True
            return self._price_reply("floor", firm=True)
        return self._price_reply("concede")

    def _within_hidden_rules(self, field: str, value: Any) -> bool:
        """Hard limits: start no later, minimum no lower, count no higher than truth."""
        tr = self.scenario.true_rules
        if field == "first_payment_date":
            return isinstance(value, date) and value <= tr.first_payment_date
        if field == "min_payment_cents":
            return isinstance(value, int) and value >= tr.min_payment_cents
        if field == "max_payments":
            return isinstance(value, int) and value <= tr.max_payments
        return False

    def _agree(self, field: str, value: Any) -> None:
        self._agreed[field] = value
        if field == "min_payment_cents":
            self.rules = dc_replace(self.rules, min_payment_cents=value)
        elif field == "max_payments":
            self.rules = dc_replace(self.rules, max_payments=value, max_terms=value)

    def _on_counter_terms(self, action: Action) -> CreditorReply:
        """Accept or reject the proposed non-price alternative under hidden rules."""
        public = self._public_facts(action)
        alt = next(
            ((_ALT_FACT_FIELDS[k], f.value) for k, f in public.items() if k in _ALT_FACT_FIELDS),
            None,
        )
        if alt is None:
            return CreditorReply(text="Which term are you asking us to change?", analysis=_acts())
        field_name, value = alt
        if self._within_hidden_rules(field_name, value):
            self._agree(field_name, value)
            text = self._say("terms_yes")
            return CreditorReply(text=text, analysis=_acts(_respond("accept", text)))
        text = self._say("terms_no")
        return CreditorReply(text=text, analysis=_acts(_respond("reject", text)))

    def _decide(self, action: Action) -> CreditorReply:
        intent = action.intent

        if intent in (
            Intent.PROPOSE_WRAP,
            Intent.CLOSE,
            Intent.NO_DEAL_WRAP,
            Intent.ESCALATE,
        ):
            self._done = True
            text = self._say("goodbye")
            return CreditorReply(text=text, analysis=_acts(EndCallAct(type="end_call", quote=text)))

        pressure = self._maybe_pressure()
        if pressure is not None:
            return pressure

        if intent == Intent.OPENING:
            text, terms = self._reveal_structure_line()
            return CreditorReply(
                text=text,
                analysis=_acts(*terms),
            )

        if intent == Intent.ASK and action.reason == "optional_sweep":
            return self._sweep_reply()

        if intent == Intent.ASK and action.reason == "min_payment_tiers":
            defining = self._define_tiers_reply()
            if defining is not None:
                return defining

        if intent == Intent.ASK:
            # Contradictory persona may flip an already-revealed max/min here.
            field = action.text_slots.get("field") or action.reason or "max_payments"
            text, terms = self._reveal_field(field)
            return CreditorReply(
                text=text,
                analysis=_acts(*terms),
            )

        if intent == Intent.ASK_SETTLEMENT:
            defining = self._define_tiers_reply()
            if defining is not None:
                return defining
            # Opportunity for contradictory flip before stating ask.
            if (
                self.persona.contradict_once
                and not self._contradicted
                and "max_payments" in self._revealed
            ):
                text, terms = self._reveal_structure_line()
                return CreditorReply(
                    text=text,
                    analysis=_acts(*terms),
                )
            return self._ask_settlement_reply()

        if intent == Intent.READ_BACK:
            field = action.text_slots.get("field") or action.reason or ""
            true_v = self._rule_value(field) if field else None
            spoken = None
            if field == "min_payment_tiers":
                # Per-tier ordinal + money facts; none means "no special tiers".
                spoken = _spoken_tiers(action, "readback_value")
            elif "readback_value" in action.facts:
                spoken = action.facts["readback_value"].value
            elif "readback_value" in action.text_slots:
                spoken = action.text_slots["readback_value"]
            # Enum read-backs arrive as text slots (``str(value)``).
            if isinstance(spoken, str) and not isinstance(true_v, str):
                matches = true_v is not None and spoken == str(true_v)
            else:
                matches = spoken is not None and spoken == true_v
            if matches:
                value = self._readback_value_phrase(field, true_v)
                if value is not None and self._lines.random() < 0.5:
                    text = self._say("readback_yes_value", v=value)
                else:
                    text = self._say("readback_yes")
                rb = "confirm"
            else:
                text = self._say("readback_no")
                rb = "deny"
            return self._with_late_fields(
                CreditorReply(
                    text=text,
                    analysis=_acts(
                        ReadbackReplyAct(type="readback_reply", response=rb, quote=text)  # type: ignore[arg-type]
                    ),
                )
            )

        if intent == Intent.CLARIFY:
            # Settle on the real (agreed) value; no further flips.
            field = action.text_slots.get("field") or action.reason or "max_payments"
            if field in ("tiers_ambiguous", "tiers_inconsistent"):
                field = "min_payment_tiers"
            self._contradicted = True
            text, terms = self._reveal_field(field)
            return CreditorReply(
                text=text,
                analysis=_acts(*terms),
            )

        if intent == Intent.COUNTER:
            return self._on_counter(action)

        if intent == Intent.COUNTER_TERMS:
            return self._on_counter_terms(action)

        if intent in (Intent.CONFIRM_SCHEDULE, Intent.SPEAK_SCHEDULE):
            # Schedule read-back: same validation for both (F15).
            return self._with_late_fields(self._check_confirm(action))

        if intent in (Intent.REFUSE_PRIVATE, Intent.REFUSE_COMMIT):
            # Continue discovery / negotiation after a refuse.
            if "max_payments" not in self._revealed:
                text, terms = self._reveal_structure_line()
                return CreditorReply(
                    text=text,
                    analysis=_acts(*terms),
                )
            return self._ask_settlement_reply()

        text, terms = self._reveal_structure_line()
        return CreditorReply(
            text=text,
            analysis=_acts(*terms),
        )

    async def _phrase(self, draft: CreditorReply, agent_text: str) -> CreditorReply:
        if self.phrasing == "template" or self.llm is None:
            return draft
        style = self.persona.name
        messages = [
            {
                "role": "system",
                "content": (
                    f"You are a creditor collections rep. Persona: {style}. "
                    "Rewrite the draft reply in that style, in plain spoken English. "
                    "Keep every number, amount, date and percentage exactly as written, "
                    "and add no other numbers. Keep the same yes or no. "
                    "One or two short sentences. Add no questions or details that are "
                    "not in the draft. Do not invent commitment, lock-in, "
                    "guarantee, or deal language."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Agent said: {agent_text}\n"
                    f"Draft reply (keep figures): {draft.text}"
                ),
            },
        ]
        self.rewrite_attempts += 1
        try:
            line = await self.llm.chat_text("sim", messages, max_tokens=SIM_MAX_TOKENS)
            line = (line or "").strip()
        except LLMUnavailable:
            raise
        except Exception:
            self.rewrite_fallbacks["error"] += 1
            return draft
        reason: FallbackReason | None
        if _COMMIT_PHRASE_RE.search(line) and not _COMMIT_PHRASE_RE.search(draft.text):
            reason = "commit"
        else:
            reason = rewrite_problem(draft.text, line, stance=_stance_of(draft.analysis))
        if reason is not None:
            # Free models change figures and flip stances: speak the code-built draft.
            self.rewrite_fallbacks[reason] += 1
            return draft
        return CreditorReply(text=line, analysis=draft.analysis)

    async def respond(self, action: Action, agent_text: str = "") -> CreditorReply:
        """Produce the next creditor utterance + oracle analysis for ``action``."""
        if self._done:
            return CreditorReply(
                text="Goodbye.", analysis=_acts(EndCallAct(type="end_call", quote="Goodbye."))
            )
        self._turn += 1
        # Strip non-public facts defensively (sim must not rely on PRIVATE).
        safe = action.model_copy(
            update={"facts": self._public_facts(action)},
        )
        draft = self._decide(safe)
        return await self._phrase(draft, agent_text)
