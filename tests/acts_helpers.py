"""Test helper: build a ``RepTurn`` (the rep's verified acts) from short keywords.

Policy and orchestrator tests describe a rep line by what it does ("accepts",
"asks 45%", "states max payments 6"); ``acts_turn`` turns that into the act
list the policy reads, with the same mapping the simulator uses (an accept /
reject is a ``respond_to_offer`` act, a price a ``price_ask``, a firm price a
``final`` one, and so on). ``rule_act`` builds one ``state_rule`` act;
``acts_reply`` / ``ActsFakeLLM`` turn a keyword reply into the model's acts JSON
for tests that drive the live NLU path. Tests only; the app never builds acts
from keywords.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from app.domain.nlu_types import (
    AnnounceRulesAct,
    AskPrivateInfoAct,
    DemandCommitmentAct,
    EndCallAct,
    HostilityAct,
    ListDoneAct,
    PriceAskAct,
    QuestionAct,
    ReadbackReplyAct,
    RepTurn,
    RespondToOfferAct,
    StallAct,
    StateRuleAct,
    UnclearAct,
)
from app.llm.client import FakeLLM


def rule_act(field: str, value: Any, quote: str, hedged: bool = False) -> StateRuleAct:
    """One stated rule (``state_rule``) with its verbatim quote."""
    return StateRuleAct(type="state_rule", field=field, value=value, quote=quote, hedged=hedged)  # type: ignore[arg-type]


def acts_turn(
    *,
    stance: str = "other",
    terms: Iterable[StateRuleAct] = (),
    settlement_ask_pct: float | None = None,
    ask_quote: str | None = None,
    settlement_ask_total_cents: int | None = None,
    ask_total_quote: str | None = None,
    amount_ambiguous_cents: int | None = None,
    amount_ambiguous_quote: str | None = None,
    readback_response: str | None = None,
    asks_client_private_info: bool = False,
    demands_commitment: bool = False,
    hostility: float = 0.0,
    wants_to_end: bool = False,
    asks_for_schedule: bool = False,
    firm: bool = False,
    tiers_ambiguous: bool = False,
    asks_question: bool = False,
    question_topic: str | None = None,
    defines_field: str | None = None,
    expected_count: int | None = None,
    list_done: bool = False,
    unaccounted_numbers: Iterable[str] = (),
    unclear: bool = False,
    quote: str = "line",
) -> RepTurn:
    """A ``RepTurn`` whose acts say what the keywords say (see the module docstring)."""
    acts: list[Any] = []
    if stance in ("accept", "reject"):
        acts.append(RespondToOfferAct(type="respond_to_offer", response=stance, quote=quote))  # type: ignore[arg-type]
    elif stance == "stall":
        acts.append(StallAct(type="stall", quote=quote))
    acts.extend(terms)
    final = "final" if firm else "counter"
    if settlement_ask_pct is not None:
        acts.append(
            PriceAskAct(
                type="price_ask", pct=settlement_ask_pct, quote=ask_quote or quote, firmness=final
            )
        )
    if settlement_ask_total_cents is not None:
        acts.append(
            PriceAskAct(
                type="price_ask",
                total_cents=settlement_ask_total_cents,
                quote=ask_total_quote or quote,
                firmness=final,
            )
        )
    if firm and settlement_ask_pct is None and settlement_ask_total_cents is None:
        acts.append(PriceAskAct(type="price_ask", firmness="final", quote=quote))
    if readback_response is not None:
        acts.append(
            ReadbackReplyAct(type="readback_reply", response=readback_response, quote=quote)  # type: ignore[arg-type]
        )
    if asks_client_private_info:
        acts.append(AskPrivateInfoAct(type="ask_private_info", quote=quote))
    if demands_commitment:
        acts.append(DemandCommitmentAct(type="demand_commitment", quote=quote))
    if hostility > 0:
        level = "strong" if hostility >= 0.8 else "mild"
        acts.append(HostilityAct(type="hostility", level=level, quote=quote))  # type: ignore[arg-type]
    if wants_to_end:
        acts.append(EndCallAct(type="end_call", quote=quote))
    if asks_for_schedule:
        acts.append(QuestionAct(type="question", topic="schedule", quote=quote))
    if asks_question:
        acts.append(QuestionAct(type="question", topic=question_topic or "other", quote=quote))  # type: ignore[arg-type]
    if defines_field is not None:
        acts.append(
            AnnounceRulesAct(
                type="announce_rules", field=defines_field, count=expected_count, quote=quote  # type: ignore[arg-type]
            )
        )
    if list_done:
        acts.append(ListDoneAct(type="list_done", quote=quote))
    if unclear:
        acts.append(UnclearAct(type="unclear", quote=quote))
    return RepTurn(
        acts=acts,
        unaccounted_numbers=list(unaccounted_numbers),
        tiers_ambiguous=tiers_ambiguous,
        amount_ambiguous_cents=amount_ambiguous_cents,
        amount_ambiguous_quote=amount_ambiguous_quote,
    )


_KEYWORDS = frozenset(
    {
        "stance", "terms", "settlement_ask_pct", "ask_quote", "settlement_ask_total_cents",
        "ask_total_quote", "amount_ambiguous_cents", "amount_ambiguous_quote",
        "readback_response", "asks_client_private_info", "demands_commitment", "hostility",
        "wants_to_end", "asks_for_schedule", "firm", "tiers_ambiguous", "asks_question",
        "question_topic", "defines_field", "expected_count", "list_done",
    }
)  # fmt: skip


def acts_reply(utterance: str, reply: dict[str, Any]) -> str:
    """The model's acts JSON for a keyword-form reply about ``utterance``.

    Acts with no span of their own quote the whole line (so they pass the quote
    check); rules and prices keep their own quotes. An ambiguous amount becomes
    a dollar-total price act (the model has no ambiguous-amount act; code
    decides what to ask).
    """
    kw = {k: v for k, v in reply.items() if v is not None}
    unknown = set(kw) - _KEYWORDS
    assert not unknown, f"not a keyword reply: {sorted(unknown)}"
    terms = [rule_act(**t) for t in kw.pop("terms", [])]
    if "amount_ambiguous_cents" in kw:
        kw.setdefault("settlement_ask_total_cents", kw.pop("amount_ambiguous_cents"))
        kw.setdefault("ask_total_quote", kw.pop("amount_ambiguous_quote", None))
    kw.pop("amount_ambiguous_quote", None)
    kw.pop("tiers_ambiguous", None)
    turn = acts_turn(**kw, terms=terms, quote=utterance.strip() or "-")
    return json.dumps({"acts": [a.model_dump(mode="json") for a in turn.acts]})


_REP_LINE = re.compile(r"^Rep: (.*)$", re.MULTILINE)


class ActsFakeLLM(FakeLLM):
    """``FakeLLM`` whose ``nlu`` replies may be written as keyword dicts.

    A queued ``nlu`` reply that is a dict (or JSON object) without ``acts`` is
    turned into the acts reply for the line in the prompt (``acts_reply``);
    anything else is returned as queued. Tests only.
    """

    async def chat_text(
        self,
        role: Any,
        messages: Any,
        max_tokens: int,
        *,
        json_mode: bool = False,
    ) -> str:
        text = await super().chat_text(role, messages, max_tokens, json_mode=json_mode)
        if role != "nlu":
            return text
        try:
            data = json.loads(text)
        except ValueError:
            return text
        if not isinstance(data, dict) or "acts" in data:
            return text
        m = _REP_LINE.search(str(messages[-1]["content"]))
        return acts_reply(m.group(1) if m else "-", data)


def flag(v: Any, name: str) -> Any:
    """What a verified turn says, read off its acts, under the old flag names (tests)."""
    acts = list(v.acts)
    if name == "hostility":
        from app.domain.nlu_types import HOSTILITY_LEVEL

        return max((HOSTILITY_LEVEL[a.level] for a in acts if a.type == "hostility"), default=0.0)
    if name in ("defines_field", "expected_count"):
        ann = next((a for a in acts if a.type == "announce_rules"), None)
        if ann is None:
            return None
        return ann.field if name == "defines_field" else ann.count
    if name in ("asks_question", "question_topic"):
        q = next((a for a in acts if a.type == "question" and a.topic != "schedule"), None)
        return (q is not None) if name == "asks_question" else (q.topic if q else None)
    if name == "firm":
        return any(a.type == "price_ask" and a.firmness == "final" for a in acts)
    if name == "asks_for_schedule":
        return any(a.type == "question" and a.topic == "schedule" for a in acts)
    kind = {
        "wants_to_end": "end_call",
        "asks_client_private_info": "ask_private_info",
        "demands_commitment": "demand_commitment",
        "unclear": "unclear",
        "list_done": "list_done",
    }[name]
    return any(a.type == kind for a in acts)


def verify_turn(turn: RepTurn, utterance: str, **kw: Any) -> Any:
    """Verify a keyword-built turn as the oracle path does (``verify_acts(oracle=True)``)."""
    from app.agent.nlu import verify_acts

    amb = (
        (turn.amount_ambiguous_cents, turn.amount_ambiguous_quote)
        if turn.amount_ambiguous_cents is not None
        else None
    )
    return verify_acts(turn.acts, utterance, oracle=True, amount_ambiguous=amb, **kw)
