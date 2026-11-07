"""NLU acts: parse the model's act list and verify it in code.

The ``nlu`` call returns ``{"acts": [...]}`` (``app.domain.nlu_types.ActList``).
This module:

1. ``parse_acts``: strict validation; a reply that fails keeps every valid
   act it holds, and only an empty salvage becomes one ``unclear`` act.
2. ``verify_acts``: the one verification path for model acts and the
   simulator's oracle acts. Rules and price go through ``verify.post_verify``
   (quote / number / range checks); tier ``rule_item``s are re-read by code
   from the rep's words (``parse_tier_item``) and must be contiguous within
   the line; every other act goes through ``cues.repair_acts``. Context rule:
   "that's all" while a list is being collected is never a goodbye.
3. Number accounting (model acts): every number in the line must sit inside
   the quote of a verified act; the rest go to ``unaccounted_numbers`` and the
   policy will not act on an older value that turn.

Does not call the model (``llm_call.analyze`` does), does not update belief and
does not decide (``app.agent.policy`` reads ``VerifiedAnalysis.to_rep_turn()``).
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.agent.nlu.amounts import (
    _COUNT_CAP_RE,
    _EXACT_COUNT_RE,
    bare_amount_ask,
    total_shape_trigger,
)
from app.agent.nlu.context import CallContext
from app.agent.nlu.cues import (
    _INJECTION_RE,
    _REVISION_RE,
    repair_acts,
    repair_stance,
    repair_wants_to_end,
)
from app.agent.nlu.fast_paths import _field_for_ask_line
from app.agent.nlu.models import (
    DroppedTerm,
    RawReading,
    RawTerm,
    VerifiedAnalysis,
    VerifiedTerm,
)
from app.agent.nlu.quotes import normalize_for_quote, quote_in_utterance
from app.agent.nlu.verify import post_verify
from app.agent.numbers import extract_tokens
from app.domain.nlu_types import (
    Act,
    ActList,
    AnnounceRulesAct,
    AskPrivateInfoAct,
    EndCallAct,
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
_MIN = "min_payment_cents"
# A figure stated as the whole settlement ("comes to $900 total", "to settle").
_TOTAL_WORD_RE = re.compile(
    r"\b(?:total|in full|altogether|all in|to settle|settle (?:it |this |the account )?for"
    r"|lump[- ]sum|comes? to|works? out to)\b",
    re.IGNORECASE,
)
_BALANCE_RE = re.compile(r"\b(?:balance|owes?|owed|owing|outstanding)\b", re.IGNORECASE)
# Per-payment cues (amounts.py's set plus "payments of $X").
_PER_PAYMENT_RE = re.compile(
    r"\b(?:per (?:payment|month|installment)|each|every|a month|monthly|minimum"
    r"|at least|no less than|a payment|(?:payments?|installments?) of)\b",
    re.IGNORECASE,
)


_ACT_ADAPTER: TypeAdapter[Any] = TypeAdapter(Act)


def parse_acts(text: str) -> tuple[ActList, str | None]:
    """``(acts, error)``: the validated list, or one ``unclear`` act and the error text.

    A reply that fails as a whole keeps every complete, valid act object it
    holds (71a-2: a reply cut at the token cap, or one malformed act, used to
    turn the whole line into ``unclear``); only when none survives is it one
    ``unclear``. The error is still returned so the caller audits it.
    """
    try:
        data = json.loads(strip_json_fences(text))
        return ActList.model_validate(data), None
    except (ValidationError, ValueError, TypeError) as e:
        salvaged = _salvage_acts(text)
        if salvaged:
            return ActList(acts=salvaged), f"salvaged {len(salvaged)}: " + str(e)[:280]
        return ActList(acts=[UnclearAct(type="unclear")]), str(e)[:300]


def _salvage_acts(text: str) -> list[Any]:
    """Each complete JSON object after ``"acts": [`` that validates as one act (max 8)."""
    start = text.find("[", max(text.find('"acts"'), 0))
    if start < 0:
        return []
    decoder = json.JSONDecoder()
    out: list[Any] = []
    i = start + 1
    while len(out) < 8:
        i = text.find("{", i)
        if i < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            break  # cut mid-object: keep what came before
        i = end
        try:
            out.append(_ACT_ADAPTER.validate_python(obj))
        except ValidationError:
            continue
    return out


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


# --- verification ---------------------------------------------------------

_YES_NO_RE = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|no|nope|nah|sure|correct|right|okay|ok)\b", re.IGNORECASE
)

# Trace / corpus label from the acts present, strongest first.
_STANCE_ORDER = ("accept", "price", "reject", "question", "info", "stall")


def _stance(kinds: set[str], *, opening: bool, no_acts: bool, utterance: str) -> str:
    """Trace / corpus stance label for the acts present (the flat label definitions).

    Reporting only (the policy reads acts). An injection attempt with no rule
    or price is ``other``. A price is an ``offer`` only in reply to our
    opening; a rule given as a change request ("make it eight instead") is a
    ``counter``; a question mark with no stronger act is a ``question``; a line
    with no act at all is a plain statement (``info``) unless it closes the call.
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


def _kind(act: Any) -> str | None:
    """The label kind an act contributes to ``_stance``."""
    if isinstance(act, (TermAnswerAct, StateRuleAct, AnnounceRulesAct, RuleItemAct, ListDoneAct)):
        return "info"
    if isinstance(act, PriceAskAct):
        return "price" if act.pct is not None or act.total_cents is not None else None
    if isinstance(act, RespondToOfferAct):
        return act.response
    if isinstance(act, (QuestionAct, AskPrivateInfoAct)):
        return "question"
    if isinstance(act, StallAct):
        return "stall"
    return None


def verify_acts(
    acts: ActList | list[Any],
    utterance: str,
    *,
    context: CallContext | None = None,
    last_agent_line: str = "",
    pending_readback: str | None = None,
    ref: date | None = None,
    audit: AuditLog | None = None,
    call_id: str | None = None,
    oracle: bool = False,
    amount_ambiguous: tuple[int, str | None] | None = None,
) -> VerifiedAnalysis:
    """Model or oracle acts → ``VerifiedAnalysis``: checked content plus repaired acts.

    1. Acts whose quote is not in the line are dropped (model acts only).
    2. Rules and price go through ``verify.post_verify`` (quote / number /
       range checks); tier ``rule_item``s are re-read from the rep's words
       and must be contiguous within the line.
    3. The other acts go through ``cues.repair_acts`` (accept / reject
       phrases, private-info and commitment cues, hostility floor, closing vs
       "that's all", schedule, firmness, questions, announcements).
    4. Model acts only: the amount readers (a dollar total the model left
       out, a bare amount beside an exact count, a total the code would
       question) and number accounting (``unaccounted_numbers``). Oracle acts
       are ground truth for what the rep meant, so nothing second-guesses
       their reading; the checks in 2 and 3 apply to both, as they always did.
       ``amount_ambiguous`` is the oracle's own "unclear amount" note
       (``RepTurn.amount_ambiguous_*``), checked like any amount.
    """
    ref_d = ref or date.today()
    ctx = context or CallContext()
    dropped: list[DroppedTerm] = []
    act_list = list(acts.acts) if isinstance(acts, ActList) else list(acts)

    def _log(event: str, payload: dict[str, Any]) -> None:
        if audit is not None and call_id is not None:
            audit.append(call_id, "nlu", event, payload)

    kept: list[Any] = []
    unclear = False
    for act in act_list:
        if isinstance(act, UnclearAct):
            unclear = True
            continue
        if not oracle and not quote_in_utterance(act.quote, utterance):
            _log("nlu_act_rejected_quote", {"type": act.type, "quote": act.quote})
            dropped.append(
                DroppedTerm(field=act.type, reason="act_rejected_quote", quote=act.quote)
            )
            continue
        kept.append(act)

    raw = RawReading()
    if amount_ambiguous is not None:
        raw.amount_ambiguous_cents, raw.amount_ambiguous_quote = amount_ambiguous
    kinds: set[str] = set()
    intent: list[Any] = []
    items: list[tuple[RuleItemAct, tuple[int, int | None, int]]] = []
    opening = not last_agent_line
    readback_field = pending_readback or (
        ctx.awaiting_field if ctx.awaiting == "readback" else None
    )
    for act in kept:
        kind = _kind(act)
        if kind is not None:
            kinds.add(kind)
        if isinstance(act, (TermAnswerAct, StateRuleAct)):
            raw.terms.append(
                RawTerm(field=act.field, value=act.value, quote=act.quote, hedged=act.hedged)
            )
        elif isinstance(act, RuleItemAct):
            parsed = parse_tier_item(act.quote, ref=ref_d) or parse_tier_item(
                _sentence_with(act.quote, utterance), ref=ref_d
            )
            if parsed is None and oracle and act.from_payment and act.min_cents:
                parsed = (act.from_payment, act.to_payment, act.min_cents)
            if parsed is None:
                _log("nlu_rejected_rule_item", {"quote": act.quote})
                dropped.append(
                    DroppedTerm(field=_TIERS, reason="rejected_rule_item", quote=act.quote)
                )
                continue
            items.append((act, parsed))
        elif isinstance(act, PriceAskAct):
            if act.pct is not None:
                raw.settlement_ask_pct, raw.ask_quote = act.pct, act.quote
            elif act.total_cents is not None:
                raw.settlement_ask_total_cents, raw.ask_total_quote = act.total_cents, act.quote
            if act.firmness == "final":
                # Firmness rides on the verified price (``to_rep_turn``).
                intent.append(PriceAskAct(type="price_ask", firmness="final", quote=act.quote))
        else:
            intent.append(act)
            if (
                isinstance(act, ReadbackReplyAct)
                and act.correction is not None
                and readback_field is not None
            ):
                raw.terms.append(
                    RawTerm(field=readback_field, value=act.correction, quote=act.quote)
                )
    no_acts = not kept and not unclear
    label = _stance(kinds, opening=opening, no_acts=no_acts, utterance=utterance)

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
    terms = list(verified.terms)
    has_terms = bool(
        terms
        or verified.settlement_ask_pct is not None
        or verified.cents_ambiguity_bare is not None
        or verified.tiers_ambiguous
        or verified.settlement_ask_total_cents is not None
        or verified.amount_ambiguous_cents is not None
        or items
    )
    repaired, levels = repair_acts(
        intent,
        utterance,
        has_terms=has_terms,
        given_fields={t.field for t in terms},
        collecting=ctx.collecting is not None,
        model_ended=any(isinstance(a, EndCallAct) for a in kept),
    )
    if levels is not None:
        # A count of payment levels is the level limit itself, read by code.
        terms.append(
            VerifiedTerm(field="max_segments", value=levels, quote=utterance.strip(), verified=True)
        )
        _log("nlu_levels_from_count", {"value": levels, "utterance": utterance[:120]})
    update["terms"] = terms

    # Trace label: the same cue repair; a price that did not verify is no counter.
    asked = (
        verified.settlement_ask_pct is not None
        or verified.settlement_ask_total_cents is not None
        or verified.amount_ambiguous_cents is not None
    )
    if "price" in kinds and not asked:
        label = _stance(kinds - {"price"}, opening=opening, no_acts=False, utterance=utterance)
    update["stance"] = repair_stance(label, utterance, has_terms=has_terms, opening=opening)
    verified = verified.model_copy(update=update)
    update = {}

    if not oracle:
        verified, update = _model_amount_readers(
            verified, kept, utterance, ctx=ctx, last_agent_line=last_agent_line,
            pending_readback=pending_readback, ref=ref_d,
        )

    # Tier items: code-read values, contiguous ranges, one tiers term for the line.
    claimed: list[str] = []
    if items:
        ranges = [p for _, p in items]
        if _contiguous(ranges):
            tiers = sorted({(frm, cents) for frm, _, cents in ranges})
            update["terms"] = [
                *(t for t in update.get("terms", verified.terms) if t.field != _TIERS),
                VerifiedTerm(
                    field=_TIERS,
                    value=tiers,
                    quote=" / ".join(a.quote for a, _ in items),
                    verified=False,
                ),
            ]
            update["tier_items"] = sorted(ranges)
            # The line gave the tiers themselves, not an ambiguous "first N".
            update["tiers_ambiguous"] = False
            claimed += [_sentence_with(a.quote, utterance) for a, _ in items]
            _log("nlu_rule_items", {"tiers": tiers})
        else:
            update["tiers_ambiguous"] = True
            _log("nlu_tiers_noncontiguous", {"items": ranges})

    final = verified.model_copy(update=update)
    unaccounted: list[str] = []
    if not oracle:
        claimed += _claimed_quotes(kept, final, repaired)
        unaccounted = _unaccounted(utterance, claimed, ref=ref_d)
        if unaccounted:
            _log(
                "nlu_unaccounted_numbers",
                {"numbers": unaccounted, "utterance": utterance[:120]},
            )
    nothing = not kept and not final.terms and final.settlement_ask_pct is None
    # A yes / no is never unintelligible: the code-side readers (read-back, the
    # sweep, list done) handle it, so it is not sent back as "say it again".
    if _YES_NO_RE.match(utterance):
        nothing = False
    if unclear and nothing:
        repaired = [*repaired, UnclearAct(type="unclear", quote=utterance.strip()[:300])]
    return final.model_copy(
        update={
            "acts": repaired,
            "unaccounted_numbers": unaccounted,
            "model_acts": [a.type for a in kept] + (["unclear"] if unclear else []),
        }
    )


def _model_amount_readers(
    verified: VerifiedAnalysis,
    kept: list[Any],
    utterance: str,
    *,
    ctx: CallContext,
    last_agent_line: str,
    pending_readback: str | None,
    ref: date,
) -> tuple[VerifiedAnalysis, dict[str, Any]]:
    """71a-2 amount readers for model acts (the act set has no ambiguous-amount act).

    - a dollar total the model left out next to a price act ("Fifty percent,
      which comes to $900 total"): a money figure no act claimed, with a total
      cue in its sentence, becomes the total ask (money in a percent-only price
      quote does not count as claimed);
    - a "minimum" read from a bare amount beside an exact payment count with no
      per-payment cue ("Three payments, $350.") is ambiguous, unless we asked
      for the minimum;
    - a dollar "total" the code would question anyway (an exact payment count
      or "pay $X by <date>" beside it, or a bare demanded amount) is ambiguous.
    """
    update: dict[str, Any] = {}
    money_quotes = [
        a.quote for a in kept if not (isinstance(a, PriceAskAct) and a.total_cents is None)
    ]
    found = _unclaimed_total(verified, utterance, money_quotes, ref=ref)
    if found is not None:
        verified = verified.model_copy(
            update={"settlement_ask_total_cents": found[0], "ask_total_quote": found[1]}
        )
    asked_min = _MIN in (
        pending_readback,
        ctx.awaiting_field,
        _field_for_ask_line(last_agent_line),
    )
    bare_min = None if asked_min else _bare_min_with_count(verified, utterance)
    if bare_min is not None and verified.amount_ambiguous_cents is None:
        update |= {
            "terms": [t for t in verified.terms if t is not bare_min],
            "amount_ambiguous_cents": bare_min.value,
            "amount_ambiguous_quote": bare_min.quote,
        }
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
    return verified, update


def _unclaimed_total(
    v: VerifiedAnalysis, utterance: str, quotes: list[str], *, ref: date
) -> tuple[int, str] | None:
    """``(cents, raw)`` of a dollar total with a total cue that no act quote or term holds."""
    if v.settlement_ask_total_cents is not None or v.amount_ambiguous_cents is not None:
        return None
    held = {t.value for t in v.terms if isinstance(t.value, int)}
    for q in quotes:
        held |= {t.value for t in extract_tokens(q, ref=ref) if t.kind == "money"}
    for sentence in re.split(r"(?<=[.!?;])\s+", utterance):
        if not _TOTAL_WORD_RE.search(sentence) or _BALANCE_RE.search(sentence):
            continue
        money = [t for t in extract_tokens(sentence, ref=ref) if t.kind == "money"]
        if len(money) == 1 and money[0].value not in held:
            return int(money[0].value), money[0].raw
    return None


def _bare_min_with_count(v: VerifiedAnalysis, utterance: str) -> VerifiedTerm | None:
    """The ``min_payment_cents`` term read from a bare amount beside an exact count."""
    term = next((t for t in v.terms if t.field == _MIN), None)
    if term is None:
        return None
    for sentence in re.split(r"(?<=[.!?;])\s+", utterance):
        money = [t for t in extract_tokens(sentence) if t.kind == "money"]
        if not any(t.value == term.value for t in money):
            continue
        if _PER_PAYMENT_RE.search(sentence) or not _EXACT_COUNT_RE.search(sentence):
            return None
        if _COUNT_CAP_RE.search(sentence):
            return None
        return term
    return None


def _claimed_quotes(kept: list[Any], v: VerifiedAnalysis, repaired: list[Any]) -> list[str]:
    """Quotes whose numbers a verified act accounts for."""
    out = [t.quote for t in v.terms if t.field != _TIERS]
    out += [q for q in (v.ask_quote, v.ask_total_quote, v.amount_ambiguous_quote) if q]
    announced = {a.field for a in repaired if isinstance(a, AnnounceRulesAct)}
    replied = any(isinstance(a, ReadbackReplyAct) for a in repaired)
    for act in kept:
        if isinstance(act, AnnounceRulesAct) and act.field in announced:
            out.append(act.quote)
        elif isinstance(act, ReadbackReplyAct) and replied:
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
