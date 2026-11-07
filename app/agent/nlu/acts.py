"""NLU v2 acts: parse the model's act list, verify it in code, convert it for the policy.

With ``NLU_SCHEMA=acts`` the ``nlu`` call returns ``{"acts": [...]}``
(``app.domain.nlu_types.ActList``). This module:

1. ``parse_acts``: strict validation; any failure becomes one ``unclear`` act
   (audited by the caller), never a crash.
2. ``convert_acts``: drops acts whose quote is not in the line, maps the rest
   onto a raw ``TurnAnalysis`` and runs the same ``verify.post_verify`` as the
   flat schema (quote / number / range checks, cue repairs). Tier
   ``rule_item``s skip the model's numbers: code re-reads each from the rep's
   words (``parse_tier_item``) and checks that ranges in one line are
   contiguous. Context rules the flat schema could not express: ``list_done``
   while a list is being collected is never ``wants_to_end``; an ``unclear``
   act asks the rep to rephrase.
3. Number accounting: every number in the line (``app.agent.numbers``) must sit
   inside the quote of a verified act; the rest go to ``unaccounted_numbers``
   and the policy will not act on an older value that turn.

Does not call the model (``llm_call.analyze`` does) and does not update belief.
Removed in 71b, when the policy reads acts directly.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.agent.nlu.amounts import bare_amount_ask, total_shape_trigger
from app.agent.nlu.context import CallContext
from app.agent.nlu.cues import (
    _INJECTION_RE,
    _REVISION_RE,
    repair_stance,
    repair_wants_to_end,
)
from app.agent.nlu.models import DroppedTerm, VerifiedAnalysis, VerifiedTerm
from app.agent.nlu.quotes import normalize_for_quote, quote_in_utterance
from app.agent.nlu.verify import post_verify
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.numbers import extract_tokens
from app.domain.nlu_types import (
    ActList,
    AnnounceRulesAct,
    AskPrivateInfoAct,
    DemandCommitmentAct,
    EndCallAct,
    HostilityAct,
    ListDoneAct,
    PriceAskAct,
    QuestionAct,
    ReadbackReplyAct,
    RespondToOfferAct,
    RuleItemAct,
    StallAct,
    StateRuleAct,
    TermAnswerAct,
    UnclearAct,
)
from app.llm.client import strip_json_fences
from app.store.audit import AuditLog

_TIERS = "min_payment_tiers"
# ``hostility`` act level → the 0..1 score the policy compares with its threshold.
_HOSTILITY_LEVEL = {"mild": 0.4, "strong": 1.0}


def parse_acts(text: str) -> tuple[ActList, str | None]:
    """``(acts, error)``: the validated list, or one ``unclear`` act and the error text."""
    try:
        data = json.loads(strip_json_fences(text))
        return ActList.model_validate(data), None
    except (ValidationError, ValueError, TypeError) as e:
        return ActList(acts=[UnclearAct(type="unclear")]), str(e)[:300]


# --- tier items -----------------------------------------------------------

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
}  # fmt: skip
_N = r"(\d{1,3}(?:st|nd|rd|th)?|" + "|".join(sorted(_WORD_NUMS, key=len, reverse=True)) + r")"
_TO = r"\s*(?:to|through|thru|-|–|and)\s*"
# Order matters: a range beats an open "from N on", which beats a single payment.
_TIER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"\b(?:the\s+)?(?:first|initial)\s+{_N}\s+payments?\b"), "first_n"),
    (re.compile(rf"\bpayments?\s+(?:numbers?\s+)?{_N}{_TO}{_N}\b"), "range"),
    (re.compile(rf"\b(?:the\s+)?{_N}{_TO}(?:the\s+)?{_N}\s+payments?\b"), "range"),
    # A bare range at the start of one item of a multi-tier line ("three to five
    # at least $250"); only tried on the item's own quote with its one amount.
    (re.compile(rf"^\W*(?:and\s+|then\s+)?{_N}{_TO}{_N}\b"), "range"),
    (
        re.compile(
            rf"\b(?:from|starting(?:\s+(?:with|at|from))?|beginning(?:\s+(?:with|at))?)"
            rf"\s+(?:the\s+)?(?:payment\s+(?:number\s+)?)?{_N}(?:\s+payment)?\b"
        ),
        "open",
    ),
    (re.compile(rf"\bpayments?\s+{_N}\s+(?:on(?:wards?)?|and\s+(?:on|after|up|beyond))\b"), "open"),
    (re.compile(rf"\bthe\s+{_N}\s+payment\b"), "single"),
)


def _num(raw: str) -> int | None:
    raw = raw.lower()
    if raw in _WORD_NUMS:
        return _WORD_NUMS[raw]
    digits = re.match(r"\d+", raw)
    return int(digits.group(0)) if digits else None


def parse_tier_item(text: str, *, ref: date | None = None) -> tuple[int, int | None, int] | None:
    """``(from_payment, to_payment | None, min_cents)`` read from the rep's words, else None.

    "the first two payments … $190" → (1, 2, 19000); "payments three to five …
    $250" → (3, 5, 25000); "from the sixth payment … $300" → (6, None, 30000).
    Needs exactly one dollar amount and one payment range in ``text``.
    """
    low = text.lower()
    money = {t.value for t in extract_tokens(text, ref=ref) if t.kind == "money"}
    if len(money) != 1:
        return None
    cents = next(iter(money))
    assert isinstance(cents, int)
    for pattern, shape in _TIER_PATTERNS:
        m = pattern.search(low)
        if m is None:
            continue
        nums = [_num(g) for g in m.groups()]
        if any(n is None or n < 1 for n in nums):
            return None
        if shape == "first_n":
            return (1, nums[0], cents)
        if shape == "range":
            a, b = nums[0], nums[1]
            assert a is not None and b is not None
            return (a, b, cents) if a <= b else None
        if shape == "open":
            assert nums[0] is not None
            return (nums[0], None, cents)
        assert nums[0] is not None
        return (nums[0], nums[0], cents)
    return None


def _sentence_with(quote: str, utterance: str) -> str:
    """The sentence of ``utterance`` that holds ``quote`` (else the whole line)."""
    q = normalize_for_quote(quote)
    for sentence in re.split(r"(?<=[.;!?])\s+", utterance):
        if q and q in normalize_for_quote(sentence):
            return sentence
    return utterance


def _contiguous(items: list[tuple[int, int | None, int]]) -> bool:
    """Ranges given in one line must not overlap and must leave no gap."""
    ordered = sorted(items)
    for (frm, to, _), (nxt, _, _) in zip(ordered, ordered[1:], strict=False):
        if nxt == frm or (to is not None and nxt != to + 1):
            return False
    return True


# --- conversion -----------------------------------------------------------

_YES_NO_RE = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|no|nope|nah|sure|correct|right|okay|ok)\b", re.IGNORECASE
)

# Stance from the acts present, strongest first (one line may carry several).
_STANCE_ORDER = ("accept", "price", "reject", "question", "info", "stall")


def _stance(kinds: set[str], *, opening: bool, no_acts: bool, utterance: str) -> str:
    """Flat-schema stance for the acts present (same label definitions as the flat prompt).

    An injection attempt with no rule or price is ``other``. A price is an
    ``offer`` only in reply to our opening; a rule given as a change request
    ("make it eight instead") is a ``counter``; a hedged rule beats a stall; a
    question mark with no stronger act is a ``question``; a line with no act at
    all is a plain statement (``info``) unless it closes the call.
    """
    if _INJECTION_RE.search(utterance) and not kinds & {"info", "price"}:
        return "other"
    if "info" in kinds and "price" not in kinds and _REVISION_RE.search(utterance):
        kinds = kinds | {"price"}  # a change request to a term is a counter
    for kind in _STANCE_ORDER:
        if kind in kinds:
            if kind == "price":
                return "offer" if opening else "counter"
            return kind
    if "?" in utterance and not kinds - {"other"}:
        return "question"
    if no_acts and not repair_wants_to_end(False, utterance):
        return "info"
    return "other"


def convert_acts(
    acts: ActList,
    utterance: str,
    *,
    context: CallContext | None = None,
    last_agent_line: str = "",
    pending_readback: str | None = None,
    ref: date | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
) -> VerifiedAnalysis:
    """Verified acts → ``VerifiedAnalysis`` (the policy's input), with number accounting."""
    ref_d = ref or date.today()
    ctx = context or CallContext()
    dropped: list[DroppedTerm] = []

    def _log(event: str, payload: dict[str, Any]) -> None:
        if audit is not None and call_id is not None:
            audit.append(call_id, "nlu", event, payload)

    kept: list[Any] = []
    unclear = False
    for act in acts.acts:
        if isinstance(act, UnclearAct):
            unclear = True
            continue
        if not quote_in_utterance(act.quote, utterance):
            _log("nlu_act_rejected_quote", {"type": act.type, "quote": act.quote})
            dropped.append(
                DroppedTerm(field=act.type, reason="act_rejected_quote", quote=act.quote)
            )
            continue
        kept.append(act)

    raw = TurnAnalysis()
    kinds: set[str] = set()
    terms: list[ExtractedTerm] = []
    items: list[tuple[RuleItemAct, tuple[int, int | None, int]]] = []
    # Same rule as the flat prompt: a price is an "offer" only after our opening.
    opening = not last_agent_line
    readback_field = pending_readback or (
        ctx.awaiting_field if ctx.awaiting == "readback" else None
    )
    for act in kept:
        if isinstance(act, (TermAnswerAct, StateRuleAct)):
            terms.append(
                ExtractedTerm(field=act.field, value=act.value, quote=act.quote, hedged=act.hedged)
            )
            kinds.add("info")
        elif isinstance(act, AnnounceRulesAct):
            if raw.defines_field is None:
                raw.defines_field, raw.expected_count = act.field, act.count
            kinds.add("info")
        elif isinstance(act, RuleItemAct):
            kinds.add("info")
            parsed = parse_tier_item(act.quote, ref=ref_d) or parse_tier_item(
                _sentence_with(act.quote, utterance), ref=ref_d
            )
            if parsed is None:
                _log("nlu_rejected_rule_item", {"quote": act.quote})
                dropped.append(
                    DroppedTerm(field=_TIERS, reason="rejected_rule_item", quote=act.quote)
                )
                continue
            items.append((act, parsed))
        elif isinstance(act, ListDoneAct):
            raw.list_done = True
            kinds.add("info")
        elif isinstance(act, PriceAskAct):
            kinds.add("price")
            if act.pct is not None:
                raw.settlement_ask_pct, raw.ask_quote = act.pct, act.quote
            else:
                raw.settlement_ask_total_cents, raw.ask_total_quote = act.total_cents, act.quote
            raw.firm = raw.firm or act.firmness == "final"
        elif isinstance(act, RespondToOfferAct):
            kinds.add(act.response)
        elif isinstance(act, ReadbackReplyAct):
            raw.readback_response = act.response
            if act.correction is not None and readback_field is not None:
                terms.append(
                    ExtractedTerm(field=readback_field, value=act.correction, quote=act.quote)  # type: ignore[arg-type]
                )
        elif isinstance(act, QuestionAct):
            raw.asks_question, raw.question_topic = True, act.topic
            kinds.add("question")
        elif isinstance(act, AskPrivateInfoAct):
            raw.asks_client_private_info = True
            kinds.add("question")
        elif isinstance(act, DemandCommitmentAct):
            raw.demands_commitment = True
        elif isinstance(act, HostilityAct):
            raw.hostility = max(raw.hostility, _HOSTILITY_LEVEL[act.level])
        elif isinstance(act, EndCallAct):
            raw.wants_to_end = True
        elif isinstance(act, StallAct):
            kinds.add("stall")
    raw.terms = terms
    no_acts = not kept and not unclear
    raw.stance = _stance(  # type: ignore[assignment]
        kinds, opening=opening, no_acts=no_acts, utterance=utterance
    )

    verified = post_verify(
        raw,
        utterance,
        ref=ref_d,
        audit=audit,
        call_id=call_id,
        last_agent_line=last_agent_line,
        pending_readback=pending_readback,
    )
    update: dict[str, Any] = {"dropped": [*dropped, *verified.dropped]}

    # A price act whose number did not verify is not a counter: re-derive the
    # stance from the acts that remain (then the same code repair).
    asked = (
        verified.settlement_ask_pct is not None
        or verified.settlement_ask_total_cents is not None
        or verified.amount_ambiguous_cents is not None
    )
    if "price" in kinds and not asked:
        stance = _stance(kinds - {"price"}, opening=opening, no_acts=False, utterance=utterance)
        update["stance"] = repair_stance(stance, utterance, has_terms=bool(verified.terms))

    # A dollar "total" the code would question anyway (an exact payment count or
    # "pay $X by <date>" beside it, or a bare demanded amount) is the flat
    # schema's ``amount_ambiguous``: same clarify downstream, same corpus label.
    total = verified.settlement_ask_total_cents
    if total is not None and verified.amount_ambiguous_cents is None:
        bare = bare_amount_ask(utterance)
        if total_shape_trigger(total, utterance) or (bare is not None and bare[0] == total):
            update |= {
                "amount_ambiguous_cents": total,
                "amount_ambiguous_quote": verified.ask_total_quote,
                "settlement_ask_total_cents": None,
                "ask_total_quote": None,
            }

    # Tier items: code-read values, contiguous ranges, one tiers term for the line.
    claimed: list[str] = []
    if items:
        ranges = [p for _, p in items]
        if _contiguous(ranges):
            tiers = sorted({(frm, cents) for frm, _, cents in ranges})
            update["terms"] = [
                *(t for t in verified.terms if t.field != _TIERS),
                VerifiedTerm(
                    field=_TIERS,
                    value=tiers,
                    quote=" / ".join(a.quote for a, _ in items),
                    verified=False,
                ),
            ]
            # The line gave the tiers themselves, not an ambiguous "first N".
            update["tiers_ambiguous"] = False
            claimed += [_sentence_with(a.quote, utterance) for a, _ in items]
            _log("nlu_rule_items", {"tiers": tiers})
        else:
            update["tiers_ambiguous"] = True
            _log("nlu_tiers_noncontiguous", {"items": ranges})

    # Context rule: "that's all" while a list is open closes the list, not the call.
    list_done = verified.list_done or raw.list_done
    collecting = ctx.collecting is not None or verified.defines_field == _TIERS
    ended = any(isinstance(a, EndCallAct) for a in kept)
    said_list = any(isinstance(a, ListDoneAct) for a in kept)
    if list_done and (collecting or (said_list and not ended)):
        update["wants_to_end"] = False
    update["list_done"] = list_done

    final = verified.model_copy(update=update)
    claimed += _claimed_quotes(kept, final)
    unaccounted = _unaccounted(utterance, claimed, ref=ref_d)
    if unaccounted:
        _log("nlu_unaccounted_numbers", {"numbers": unaccounted, "utterance": utterance[:120]})
    nothing = not kept and not final.terms and final.settlement_ask_pct is None
    # A yes / no is never unintelligible: the code-side readers (read-back, the
    # sweep, list done) handle it, so it is not sent back as "say it again".
    if _YES_NO_RE.match(utterance):
        nothing = False
    return final.model_copy(
        update={
            "unaccounted_numbers": unaccounted,
            "unclear": unclear and nothing,
            "acts": [a.type for a in kept] + (["unclear"] if unclear else []),
        }
    )


def _claimed_quotes(kept: list[Any], v: VerifiedAnalysis) -> list[str]:
    """Quotes whose numbers a verified act accounts for."""
    out = [t.quote for t in v.terms if t.field != _TIERS]
    out += [q for q in (v.ask_quote, v.ask_total_quote, v.amount_ambiguous_quote) if q]
    for act in kept:
        if isinstance(act, AnnounceRulesAct) and v.defines_field == act.field:
            out.append(act.quote)
        elif isinstance(act, ReadbackReplyAct) and v.readback_response is not None:
            out.append(act.quote)
        elif isinstance(act, (TermAnswerAct, StateRuleAct)) and (
            v.cents_ambiguity_field == act.field
            or (act.field == "max_segments" and any(t.field == act.field for t in v.terms))
        ):
            # A bare amount we will ask dollars-or-cents about is still accounted for.
            out.append(act.quote)
    return out


def _unaccounted(utterance: str, claimed: list[str], *, ref: date) -> list[str]:
    """Raw spans of numbers in the line that no claimed quote covers."""
    norm = [normalize_for_quote(q) for q in claimed if q]
    out: list[str] = []
    for tok in extract_tokens(utterance, ref=ref):
        raw = normalize_for_quote(tok.raw)
        if not raw:
            continue
        if not any(re.search(rf"(?<!\w){re.escape(raw)}(?!\w)", q) for q in norm):
            out.append(tok.raw)
    return out
