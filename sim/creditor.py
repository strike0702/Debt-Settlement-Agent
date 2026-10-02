"""Code-based creditor policy + phraser for offline/online sims.

``CreditorPolicy`` decides moves from the agent's ``Action`` intent and PUBLIC
facts plus spoken text — never PRIVATE client numbers. Accept/reject and rule
reveals are deterministic; the phraser (``sim`` LLM role or templates) only
styles the line and emits ground-truth ``TurnAnalysis`` for oracle NLU.

Must not import ``app.agent``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

from app.adapter.engine_adapter import evaluate
from app.adapter.validator import validate
from app.domain.actions import Action, Intent
from app.domain.facts import Fact
from app.domain.nlu_types import ExtractedTerm, TurnAnalysis
from app.domain.units import render_count, render_money, render_pct
from app.llm.client import LLMUnavailable
from feasibility.models import CreditorRules
from sim.personas import Persona, get_persona
from sim.scenarios import Scenario, to_creditor_rules

PhrasingMode = Literal["template", "llm"]

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


class _SimLLM(Protocol):
    async def chat_text(
        self,
        role: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> str: ...


@dataclass
class CreditorReply:
    """Spoken creditor line plus oracle ``TurnAnalysis``."""

    text: str
    analysis: TurnAnalysis


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

    def __post_init__(self) -> None:
        self.persona = get_persona(self.scenario.persona)
        self.rules = to_creditor_rules(
            self.scenario.true_rules,
            program_fee_pct=self.scenario.call.program_fee_pct,
            bank_fee_cents=self.scenario.call.bank_fee_cents,
        )
        self.current_ask_bp = self.scenario.opening_ask_bp

    @property
    def done(self) -> bool:
        return self._done

    def _public_facts(self, action: Action) -> dict[str, Fact]:
        return {
            k: v
            for k, v in action.facts.items()
            if v.visibility == "PUBLIC"
        }

    def _true_value(self, field: str) -> Any:
        tr = self.scenario.true_rules
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
    ) -> ExtractedTerm:
        return ExtractedTerm(field=field, value=value, quote=quote, hedged=hedged)  # type: ignore[arg-type]

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
                    analysis=TurnAnalysis(
                        stance="other",
                        asks_client_private_info=True,
                        demands_commitment=True,
                    ),
                )
            text = "What is the client's monthly income and bank balance?"
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(
                    stance="other",
                    asks_client_private_info=True,
                ),
            )
        if self._turn in self.persona.pressure_commit_turns:
            text = "I need a firm commitment that this deal is locked in."
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(stance="other", demands_commitment=True),
            )
        return None

    def _reveal_structure_line(self) -> tuple[str, list[ExtractedTerm]]:
        """Speak required rules (and track reveals / optional contradiction)."""
        tr = self.scenario.true_rules
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
            text = (
                f"Actually, make that a maximum of {quote_max} payments, "
                f"minimum {render_money(min_c)}, {structure} structure."
            )
            terms = [
                self._term("max_payments", new_max, quote_max),
                self._term("min_payment_cents", min_c, render_money(min_c)),
                self._term("payment_structure", structure, structure),
            ]
            return text, terms

        self._revealed.update(
            {"max_payments", "min_payment_cents", "payment_structure"}
        )
        self._spoken_max_payments = max_p
        self._spoken_min_payment = min_c
        text = (
            f"We can take up to {render_count(max_p)} payments, "
            f"minimum {render_money(min_c)}, {structure} payments."
        )
        terms = [
            self._term("max_payments", max_p, render_count(max_p)),
            self._term("min_payment_cents", min_c, render_money(min_c)),
            self._term("payment_structure", structure, structure),
        ]
        return text, terms

    def _reveal_field(self, field: str) -> tuple[str, list[ExtractedTerm]]:
        value = self._true_value(field)
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
                return f"Actually the maximum is {q} payments.", [
                    self._term(field, new_max, q)
                ]
            self._spoken_max_payments = int(value)
            q = render_count(int(value))
            return f"The maximum is {q} payments.", [self._term(field, int(value), q)]
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
                return f"Actually our minimum payment is {q}.", [
                    self._term(field, new_min, q)
                ]
            self._spoken_min_payment = int(value)
            q = render_money(int(value))
            return f"Our minimum payment is {q}.", [self._term(field, int(value), q)]
        if field == "payment_structure":
            s = str(value)
            return f"We need {s} payments.", [self._term(field, s, s)]
        if field == "first_payment_date":
            assert isinstance(value, date)
            # ISO appears in text for quote match under oracle (quote check still runs).
            iso = value.isoformat()
            return f"First payment date should be {iso}.", [
                self._term(field, value, iso)
            ]
        if field == "max_segments":
            q = render_count(int(value))
            return f"At most {q} payment levels.", [self._term(field, int(value), q)]
        if field == "max_token_pays":
            q = render_count(int(value))
            return f"At most {q} token payments.", [self._term(field, int(value), q)]
        if field == "min_payment_tiers":
            return "No special payment tiers.", [
                self._term(field, list(value) if value else [], "No special")
            ]
        return "I am not sure about that term.", []

    def _ask_settlement_reply(self) -> CreditorReply:
        pct = self.current_ask_bp / 100.0
        spoken = render_pct(self.current_ask_bp)
        text = f"We are looking for a {spoken} settlement."
        return CreditorReply(
            text=text,
            analysis=TurnAnalysis(
                settlement_ask_pct=pct,
                ask_quote=spoken,
                stance="offer",
            ),
        )

    def _check_confirm(self, action: Action) -> CreditorReply:
        """Validate CONFIRM_SCHEDULE under true rules; reveal violations."""
        public = self._public_facts(action)
        bp_fact = public.get("settlement_pct")
        if bp_fact is None or not isinstance(bp_fact.value, int):
            text = "I need a clear settlement percentage on that schedule."
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(stance="reject"),
            )
        bp = int(bp_fact.value)
        fpd = self.scenario.true_rules.first_payment_date
        summary = evaluate(self.scenario.call, self.rules, bp, fpd)
        if not summary.feasible or summary.rows is None:
            floor_m = render_money(self.scenario.true_rules.min_payment_cents)
            text = (
                f"That schedule does not work for us — "
                f"our minimum is actually {floor_m}."
            )
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(
                    stance="reject",
                    terms=[
                        self._term(
                            "min_payment_cents",
                            self.scenario.true_rules.min_payment_cents,
                            floor_m,
                        )
                    ],
                ),
            )
        violations = validate(
            summary.rows,
            self.scenario.call.client,
            summary.offer_total_cents,
            summary.program_fee_cents,
            self.rules,
            fpd,
        )
        # Spoken offer_total / num_payments must match true-rules eval.
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
            floor_m = render_money(self.scenario.true_rules.min_payment_cents)
            text = (
                f"Those payment amounts are off — "
                f"our minimum is actually {floor_m}."
            )
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(
                    stance="reject",
                    terms=[
                        self._term(
                            "min_payment_cents",
                            self.scenario.true_rules.min_payment_cents,
                            floor_m,
                        )
                    ],
                ),
            )
        if bp < self.scenario.floor_bp:
            text = (
                f"The schedule shape is fine, but we cannot go below "
                f"{render_pct(self.scenario.floor_bp)}."
            )
            self.current_ask_bp = max(
                self.scenario.floor_bp, self.current_ask_bp - 500
            )
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(
                    stance="reject",
                    settlement_ask_pct=self.current_ask_bp / 100.0,
                    ask_quote=render_pct(self.current_ask_bp),
                ),
            )
        text = "Yes, that payment schedule works for us. Agreed."
        return CreditorReply(
            text=text,
            analysis=TurnAnalysis(stance="accept", readback_response="confirm"),
        )

    def _on_counter(self, action: Action) -> CreditorReply:
        public = self._public_facts(action)
        bp_fact = public.get("counter_pct")
        if bp_fact is None or not isinstance(bp_fact.value, int):
            return CreditorReply(
                text="Could you repeat that counter offer as a percentage?",
                analysis=TurnAnalysis(stance="question"),
            )
        bp = int(bp_fact.value)
        floor = self.scenario.floor_bp
        if bp >= floor:
            text = f"We can accept {render_pct(bp)}."
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(stance="accept"),
            )
        # Concede 5 percentage points toward the floor.
        self.current_ask_bp = max(floor, self.current_ask_bp - 500)
        spoken = render_pct(self.current_ask_bp)
        text = (
            f"That is too low. The best we can do right now is {spoken}."
        )
        return CreditorReply(
            text=text,
            analysis=TurnAnalysis(
                stance="reject",
                settlement_ask_pct=self.current_ask_bp / 100.0,
                ask_quote=spoken,
            ),
        )

    def _decide(self, action: Action) -> CreditorReply:
        intent = action.intent

        if intent in (
            Intent.PROPOSE_WRAP,
            Intent.CLOSE,
            Intent.NO_DEAL_WRAP,
            Intent.ESCALATE,
        ):
            self._done = True
            return CreditorReply(
                text="Understood. Goodbye.",
                analysis=TurnAnalysis(stance="other", wants_to_end=True),
            )

        pressure = self._maybe_pressure()
        if pressure is not None:
            return pressure

        if intent == Intent.OPENING:
            text, terms = self._reveal_structure_line()
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(terms=terms, stance="info"),
            )

        if intent == Intent.ASK:
            field = action.text_slots.get("field") or action.reason or "max_payments"
            # After rules known, contradictory may spontaneously flip.
            if (
                self.persona.contradict_once
                and not self._contradicted
                and field in ("max_payments", "min_payment_cents")
                and field in self._revealed
            ):
                text, terms = self._reveal_field(field)
                return CreditorReply(
                    text=text,
                    analysis=TurnAnalysis(terms=terms, stance="info"),
                )
            text, terms = self._reveal_field(field)
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(terms=terms, stance="info"),
            )

        if intent == Intent.ASK_SETTLEMENT:
            # Opportunity for contradictory flip before stating ask.
            if (
                self.persona.contradict_once
                and not self._contradicted
                and "max_payments" in self._revealed
            ):
                text, terms = self._reveal_structure_line()
                return CreditorReply(
                    text=text,
                    analysis=TurnAnalysis(terms=terms, stance="info"),
                )
            return self._ask_settlement_reply()

        if intent == Intent.READ_BACK:
            field = action.text_slots.get("field") or action.reason or ""
            true_v = self._true_value(field) if field else None
            spoken = None
            if "readback_value" in action.facts:
                spoken = action.facts["readback_value"].value
            elif "readback_value" in action.text_slots:
                spoken = action.text_slots["readback_value"]
            matches = spoken is not None and true_v is not None and spoken == true_v
            if matches:
                text = "Yes, that is correct."
                rb = "confirm"
            else:
                text = "No, that is not correct."
                rb = "deny"
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(
                    stance="info",
                    readback_response=rb,  # type: ignore[arg-type]
                ),
            )

        if intent == Intent.CLARIFY:
            # Stick to the true rule value.
            field = action.text_slots.get("field") or action.reason or "max_payments"
            true_v = self._true_value(field)
            if field == "max_payments":
                q = render_count(int(true_v))
                text = f"Please use {q} payments — that is the correct maximum."
                terms = [self._term(field, int(true_v), q)]
            elif field == "min_payment_cents":
                q = render_money(int(true_v))
                text = f"Please use {q} as the minimum."
                terms = [self._term(field, int(true_v), q)]
            else:
                text = f"Please use {true_v}."
                terms = [self._term(field, true_v, str(true_v))]
            self._contradicted = True  # settled
            return CreditorReply(
                text=text,
                analysis=TurnAnalysis(terms=terms, stance="info"),
            )

        if intent == Intent.COUNTER:
            return self._on_counter(action)

        if intent == Intent.CONFIRM_SCHEDULE:
            return self._check_confirm(action)

        if intent == Intent.SPEAK_SCHEDULE:
            return CreditorReply(
                text="Yes I accept that payment schedule. Agreed.",
                analysis=TurnAnalysis(stance="accept"),
            )

        if intent in (Intent.REFUSE_PRIVATE, Intent.REFUSE_COMMIT):
            # Continue discovery / negotiation after a refuse.
            if "max_payments" not in self._revealed:
                text, terms = self._reveal_structure_line()
                return CreditorReply(
                    text=text,
                    analysis=TurnAnalysis(terms=terms, stance="info"),
                )
            return self._ask_settlement_reply()

        text, terms = self._reveal_structure_line()
        return CreditorReply(
            text=text,
            analysis=TurnAnalysis(terms=terms, stance="info"),
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
                    "Rewrite the draft reply in that style. Keep every number "
                    "and percentage exactly as written. One or two short sentences. "
                    "Do not invent commitment, lock-in, guarantee, or deal language."
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
        try:
            line = await self.llm.chat_text("sim", messages, max_tokens=120)
            line = (line or "").strip() or draft.text
            # Strip commitment phrasing the LLM may have injected.
            if _COMMIT_PHRASE_RE.search(line) and not _COMMIT_PHRASE_RE.search(
                draft.text
            ):
                line = draft.text
            return CreditorReply(text=line, analysis=draft.analysis)
        except LLMUnavailable:
            raise
        except Exception:
            return draft

    async def respond(self, action: Action, agent_text: str = "") -> CreditorReply:
        """Produce the next creditor utterance + oracle analysis for ``action``."""
        if self._done:
            return CreditorReply(
                text="Goodbye.",
                analysis=TurnAnalysis(stance="other", wants_to_end=True),
            )
        self._turn += 1
        # Strip non-public facts defensively (sim must not rely on PRIVATE).
        safe = action.model_copy(
            update={"facts": self._public_facts(action)},
        )
        draft = self._decide(safe)
        return await self._phrase(draft, agent_text)
