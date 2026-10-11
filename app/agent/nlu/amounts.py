"""Dollar amounts: total-vs-per-payment resolution and the "which one?" question.

``post_verify`` cannot turn a dollar total into basis points (it has no
balance), so the orchestrator calls ``resolve_amounts`` with the account
balance: a verified total becomes the settlement ask (``total_cents_to_bp``),
unless the amount may as well be per payment, in which case it returns a
pending "total or per payment?" clarify instead of guessing. The triggers and
their order are listed on ``resolve_amounts``. ``try_resolve_amount_clarify``
reads the rep's answer to that question. Integer cents and bp only.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Any

from app.agent.nlu.models import VerifiedAnalysis, VerifiedTerm
from app.agent.nlu.quotes import _in_prior_range
from app.agent.numbers import extract_tokens
from app.agent.policy import ask_pct_to_bp
from app.store.audit import AuditLog

# --- Dollar-total asks and total-vs-per-payment ambiguity -------------------
#
# ``post_verify`` cannot convert a dollar total to bp (it has no balance), so
# the orchestrator calls ``resolve_amounts`` after NLU. It either turns a
# verified total into the ordinary ``settlement_ask_pct`` or returns a pending
# clarify; it never changes an amount on its own.

# A % ask and a dollar total agree when they differ by at most half a percent
# point (the rep rounding the percent to a whole number).
_PCT_TOTAL_TOLERANCE_BP = 50

# Total cues: the amount is the whole settlement. "pay $X by <date>" is a
# deadline for a sum, not a per-payment floor.
_TOTAL_CUE_RE = re.compile(
    r"\b(?:total|in full|to settle|settle (?:it |this |the account )?for|lump[- ]sum)\b"
    r"|\bpay\s+\$\s?[\d,]+(?:\.\d{2})?\s+by\b",
    re.IGNORECASE,
)
# Per-payment cues: the amount is a floor for each payment.
_PER_PAYMENT_CUE_RE = re.compile(
    r"\b(?:per (?:payment|month|installment)|each|every|a month|monthly|minimum"
    r"|at least|a payment)\b",
    re.IGNORECASE,
)
# Answers to "Is $X the total settlement, or the minimum for each payment?".
_ANSWER_TOTAL_RE = re.compile(
    r"\b(?:total|in full|settle|settlement|whole|altogether|all in|the first|first one|lump)\b",
    re.IGNORECASE,
)
_ANSWER_PER_RE = re.compile(
    r"\b(?:per|each|every|minimum|monthly|a month|installments?|the second|second one)\b",
    re.IGNORECASE,
)


# A payment count with these words is a maximum, not the count.
_COUNT_CAP_RE = re.compile(
    r"\b(?:up to|at most|max(?:imum)?|no more than|or fewer|or less|as many as)\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"[,.;!?]|\bbut\b", re.IGNORECASE)

# Code-side question for a verified TOTAL (whatever the model said):
# in the same sentence as an exact payment count ("3 even payments"), or as
# "pay $X by <date>", the amount may as well be per payment, so ask. An
# explicit total cue in that sentence ("in total", "altogether") settles it.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
_EXPLICIT_TOTAL_RE = re.compile(
    r"\b(?:total|altogether|all together|all in|in full|lump[- ]sum|in one payment"
    r"|settle (?:it |this |the account )?for|settlement of)\b",
    re.IGNORECASE,
)
_NUMBER_WORD = (
    r"(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"(?:thir|four|fif|six|seven|eigh|nine)teen|twenty(?:[- ](?:one|two|three|four))?)"
)
_EXACT_COUNT_RE = re.compile(
    rf"\b(?:\d+|{_NUMBER_WORD})\s+(?:(?:even|equal|monthly|separate|fixed)\s+)?"
    r"(?:payments|installments|instalments)\b",
    re.IGNORECASE,
)
# A sentence that states the debt ("original balance of $6,000", "the client
# owes $6,000") is not an ask in any shape: the shape question never fires on it.
_BALANCE_STATEMENT_RE = re.compile(
    r"\b(?:balance|owes?|owed|owing|outstanding|original amount|debt of|account is at)\b",
    re.IGNORECASE,
)
# A bare dollar amount the rep demands for the deal
# ("We would need $600 from the client.", "We'd want $450 to close this.") with
# no count, date, % or total / per-payment cue in its sentence asks "total or
# per payment?" whatever the model returned. Both a demand verb and a deal
# target are required, so a short answer to our own minimum-payment question
# ("We'd need $150.") is left to the field it answers.
_DEMAND_RE = re.compile(
    r"\b(?:need|needs|want|wants|require|requires|looking for|asking for|expect"
    r"|(?:would|could|can|will) (?:take|accept|do))\b",
    re.IGNORECASE,
)
_DEAL_TARGET_RE = re.compile(
    r"\bfrom (?:the |your |their )?(?:client|consumer|debtor|borrower|customer|them|him|her)\b"
    r"|\bto (?:close|settle|resolve|clear|wrap|finish|end|take care of)\b"
    r"|\b(?:on|for) (?:this|it|the account|this account|this one|the file|this file)\b",
    re.IGNORECASE,
)
# Amounts the agent named ("the $2,000 you offered") or bounded ones ("more than
# $600") are not a new ask.
_NOT_BARE_RE = re.compile(
    r"\byou(?:'ve| have)? (?:offered|proposed|mentioned|suggested|said)\b"
    r"|\byour (?:offer|proposal|number|figure)\b"
    r"|\b(?:more than|over|above|under|below|less than|at least|at most|up to)\s+\$",
    re.IGNORECASE,
)
_PAY_BEFORE_RE = re.compile(r"\bpay\s+$", re.IGNORECASE)
_BY_AFTER_RE = re.compile(r"^\s+by\b", re.IGNORECASE)
_NEGATION_RE = re.compile(r"\b(?:not|isn't|isnt|no)\b", re.IGNORECASE)


def total_cents_to_bp(total_cents: int, balance_cents: int) -> tuple[int, Decimal]:
    """``(bp, exact)``: a dollar total as bp of the balance, rounded UP to a whole bp.

    Rounding up never favours the client: we never believe the ask is lower
    than the rep said.
    """
    exact = Decimal(total_cents) * Decimal(10000) / Decimal(balance_cents)
    return int(exact.to_integral_value(rounding=ROUND_CEILING)), exact


def bp_to_ask_pct(bp: int) -> float:
    """Inverse of ``ask_pct_to_bp`` for a whole bp (4567 → 45.67, round-trips exactly)."""
    return float(Decimal(bp) / Decimal(100))


def _amount_pending(
    cents: int, quote: str | None, trigger: str, analysis: VerifiedAnalysis
) -> dict[str, Any]:
    """Pending clarify state. A verified % ask the clarify holds back rides along."""
    pending: dict[str, Any] = {"cents": cents, "quote": quote, "trigger": trigger}
    if analysis.settlement_ask_pct is not None and analysis.ask_verified:
        pending["pct"] = analysis.settlement_ask_pct
        pending["pct_quote"] = analysis.ask_quote
    return pending


def total_shape_trigger(total_cents: int, utterance: str) -> str | None:
    """Question trigger for a verified dollar total, or None when it reads as a total.

    Looks only at the sentence(s) that say the amount. ``total_pay_by``: "pay
    $X by <date>"; ``total_with_count``: an exact payment count in the same
    sentence (a capped "up to 6 payments" is not exact). An explicit total cue
    or a balance statement ("original balance of $6,000") in that sentence
    means nothing is asked.
    """
    for sentence in _SENTENCE_SPLIT_RE.split(utterance):
        hits = [
            t
            for t in extract_tokens(sentence)
            if t.kind == "money" and t.value == total_cents
        ]
        if not hits or _EXPLICIT_TOTAL_RE.search(sentence):
            continue
        if _BALANCE_STATEMENT_RE.search(sentence):
            continue
        for tok in hits:
            if _PAY_BEFORE_RE.search(sentence[: tok.start]) and _BY_AFTER_RE.search(
                sentence[tok.end :]
            ):
                return "total_pay_by"
        if _EXACT_COUNT_RE.search(sentence) and not _COUNT_CAP_RE.search(sentence):
            return "total_with_count"
    return None


def _sentences_with_amount(cents: int, utterance: str) -> list[str]:
    return [
        sentence
        for sentence in _SENTENCE_SPLIT_RE.split(utterance)
        if any(t.kind == "money" and t.value == cents for t in extract_tokens(sentence))
    ]


def balance_statement_amount(cents: int, utterance: str) -> bool:
    """True when every sentence that says ``cents`` states the debt, not an ask.

    "Our records show an original balance of $6,000." is; "We need $2,000 to
    clear the balance." is not (a demand verb in the amount's clause makes it
    an ask, while "The client owes $6,000, and we need 3 payments" is still a
    statement). ``resolve_amounts`` uses it to drop a model's
    ``amount_ambiguous`` flag.
    """
    said = _sentences_with_amount(cents, utterance)
    return bool(said) and all(
        _BALANCE_STATEMENT_RE.search(s) and not _DEMAND_RE.search(_clause_with_amount(cents, s))
        for s in said
    )


# Clause breaks that keep "$6,000" whole (a comma only when a space follows).
_AMOUNT_CLAUSE_SPLIT_RE = re.compile(r",\s+|;|\bbut\b", re.IGNORECASE)


def _clause_with_amount(cents: int, sentence: str) -> str:
    """The comma / "but" clause of ``sentence`` that says ``cents`` (the sentence if none)."""
    for clause in _AMOUNT_CLAUSE_SPLIT_RE.split(sentence):
        if any(t.kind == "money" and t.value == cents for t in extract_tokens(clause)):
            return clause
    return sentence


def bare_amount_ask(utterance: str) -> tuple[int, str] | None:
    """``(cents, raw)`` of a bare dollar amount demanded for the deal, else None.

    The sentence holds exactly one figure (a dollar amount: no count, date or
    %), a demand verb before it and a deal target ("from the client", "to close
    this"), and no total, per-payment, balance, agent-named or bounded cue, and
    it is not a question or negated.
    """
    for sentence in _SENTENCE_SPLIT_RE.split(utterance):
        toks = extract_tokens(sentence)
        if len(toks) != 1 or toks[0].kind != "money":
            continue
        tok = toks[0]
        before = sentence[: tok.start]
        if not _DEMAND_RE.search(before) or not _DEAL_TARGET_RE.search(sentence):
            continue
        if (
            sentence.rstrip().endswith("?")
            or _EXPLICIT_TOTAL_RE.search(sentence)
            or _TOTAL_CUE_RE.search(sentence)
            or _PER_PAYMENT_CUE_RE.search(sentence)
            or _BALANCE_STATEMENT_RE.search(sentence)
            or _NOT_BARE_RE.search(sentence)
            or _NEGATION_RE.search(before)
            or re.search(r"n't\b|\bnever\b", before, re.IGNORECASE)
        ):
            continue
        return int(tok.value), tok.raw
    return None


def resolve_amounts(
    verified: VerifiedAnalysis,
    utterance: str,
    *,
    balance_cents: int,
    check_min_payment: bool = True,
    audit: AuditLog | None = None,
    call_id: str | None = None,
) -> tuple[VerifiedAnalysis, dict[str, Any] | None]:
    """Turn a verified dollar total into the ask, or return a pending clarify.

    Returns ``(analysis, pending)``. With ``pending`` set, the caller asks
    "total or per payment?" and ``analysis`` has the doubtful amount (and any
    disagreeing ask) removed so the rest of the turn can still be applied.
    Triggers, first match: NLU ``amount_ambiguous`` (not when the amount's
    sentence is a balance statement); a bare demanded amount with no
    count / date / total or per-payment cue (``bare_amount_ask``, not
    right after the rep answered this question); % and total disagree;
    total above the balance; a total with no agreeing % in a per-payment shape
    (``total_shape_trigger``: "pay $X by", or an exact count in the same
    sentence); a ``min_payment_cents`` term that is implausible
    (amount × an exact payment count said this turn, else × 1, > balance),
    sits under a total cue with no per-payment cue, or equals a total the NLU
    also gave.
    ``check_min_payment=False`` after the rep already said "per payment".
    """

    def _log(event: str, payload: dict[str, Any]) -> None:
        if audit is not None and call_id is not None:
            audit.append(call_id, "nlu", event, payload)

    def _clarify(
        cents: int, quote: str | None, trigger: str, *, drop_ask: bool = False
    ) -> tuple[VerifiedAnalysis, dict[str, Any]]:
        pending = _amount_pending(cents, quote, trigger, verified)
        update: dict[str, Any] = {
            "terms": [
                t
                for t in verified.terms
                if not (t.field == "min_payment_cents" and t.value == cents)
            ],
            "settlement_ask_total_cents": None,
            "ask_total_quote": None,
            "amount_ambiguous_cents": None,
            "amount_ambiguous_quote": None,
        }
        if drop_ask:
            update.update(settlement_ask_pct=None, ask_quote=None, ask_verified=False)
        _log("nlu_amount_ambiguous", {"cents": cents, "quote": quote, "trigger": trigger})
        return verified.model_copy(update=update), pending

    if verified.amount_ambiguous_cents is not None:
        flagged = verified.amount_ambiguous_cents
        if not balance_statement_amount(flagged, utterance):
            return _clarify(flagged, verified.amount_ambiguous_quote, "nlu_flag")
        # A balance statement is never an ask: drop the flag, no question.
        _log("nlu_amount_flag_dropped", {"cents": flagged, "reason": "balance_statement"})
        verified = verified.model_copy(
            update={"amount_ambiguous_cents": None, "amount_ambiguous_quote": None}
        )

    if check_min_payment:
        bare = bare_amount_ask(utterance)
        if bare is not None:
            return _clarify(bare[0], bare[1], "bare_amount")

    total = verified.settlement_ask_total_cents
    if total is not None and balance_cents > 0:
        bp, exact = total_cents_to_bp(total, balance_cents)
        pct_bp = (
            ask_pct_to_bp(verified.settlement_ask_pct)
            if verified.settlement_ask_pct is not None and verified.ask_verified
            else None
        )
        if pct_bp is not None and abs(exact - Decimal(pct_bp)) > _PCT_TOTAL_TOLERANCE_BP:
            return _clarify(total, verified.ask_total_quote, "pct_total_disagree", drop_ask=True)
        if bp > 10000:
            return _clarify(total, verified.ask_total_quote, "total_exceeds_balance")
        shape = total_shape_trigger(total, utterance) if pct_bp is None else None
        if shape is not None:
            return _clarify(total, verified.ask_total_quote, shape)
        chosen = bp if pct_bp is None else max(bp, pct_bp)
        _log(
            "nlu_ask_total_to_bp",
            {
                "total_cents": total,
                "balance_cents": balance_cents,
                "exact_bp": str(exact),
                "rounded_bp": bp,
                "pct_bp": pct_bp,
                "ask_bp": chosen,
            },
        )
        verified = verified.model_copy(
            update={
                "settlement_ask_pct": bp_to_ask_pct(chosen),
                "ask_quote": verified.ask_quote or verified.ask_total_quote,
                "ask_verified": True,
                "ask_total_bp": chosen,
            }
        )

    if check_min_payment:
        # "up to 12 payments" is a cap (one payment is still allowed), so only an
        # exact count said in the same turn ("only 3 even payments") multiplies.
        counts = [
            int(t.value)
            for t in verified.terms
            if t.field == "max_payments" and isinstance(t.value, int)
        ]
        exact_count = bool(counts) and not _COUNT_CAP_RE.search(utterance)
        count = min(counts) if exact_count else 1
        per_cue = bool(_PER_PAYMENT_CUE_RE.search(utterance))
        for t in verified.terms:
            if t.field != "min_payment_cents" or not isinstance(t.value, int):
                continue
            if balance_cents > 0 and t.value * count > balance_cents:
                return _clarify(t.value, t.quote, "min_exceeds_balance")
            if not per_cue and _TOTAL_CUE_RE.search(utterance):
                return _clarify(t.value, t.quote, "total_cue")
            if total is not None and t.value == total:
                return _clarify(t.value, t.quote, "both_readings")
    return verified, None


def try_resolve_amount_clarify(
    utterance: str,
    pending: dict[str, Any],
    *,
    ref: date | None = None,
) -> VerifiedAnalysis | None:
    """Resolve "total or per payment?" deterministically; ``None`` when still unclear.

    "the total" → a verified dollar-total ask (``resolve_amounts`` converts it);
    "per payment" → a verified ``min_payment_cents`` term. Either way a % ask
    the clarify held back comes back, except after a %-vs-total disagreement
    answered "the total". A restated amount counts only with one of those cues
    ("$450 total"); a bare amount, "yes", or both cues stay unclear.
    """
    text = utterance.strip()
    # "the total, not per payment": a negated clause names the reading the rep
    # rules out, so only un-negated clauses count. Unpunctuated negation stays
    # unclear (asked again) rather than guessed.
    clauses = [
        c for c in _CLAUSE_SPLIT_RE.split(text) if c.strip() and not _NEGATION_RE.search(c)
    ]
    is_total = any(_ANSWER_TOTAL_RE.search(c) for c in clauses)
    is_per = any(_ANSWER_PER_RE.search(c) for c in clauses)
    if is_total == is_per:
        return None
    ref_d = ref or date.today()
    cents = int(pending["cents"])
    quote = str(pending.get("quote") or "")
    money = [t for t in extract_tokens(text, ref=ref_d) if t.kind == "money"]
    if len(money) > 1:
        return None
    if money:
        cents, quote = int(money[0].value), money[0].raw
    if is_total:
        out = VerifiedAnalysis(
            stance="info",
            settlement_ask_total_cents=cents,
            ask_total_quote=quote or None,
        )
        # A % that disagreed with this total is what the rep just overruled.
        if pending.get("pct") is not None and pending.get("trigger") != "pct_total_disagree":
            out = out.model_copy(
                update={
                    "settlement_ask_pct": float(pending["pct"]),
                    "ask_quote": pending.get("pct_quote"),
                    "ask_verified": True,
                }
            )
        return out
    if not _in_prior_range("min_payment_cents", cents):
        return None
    out = VerifiedAnalysis(
        terms=[
            VerifiedTerm(
                field="min_payment_cents",
                value=cents,
                quote=quote,
                hedged=False,
                verified=True,
            )
        ],
        stance="info",
    )
    if pending.get("pct") is not None:
        out = out.model_copy(
            update={
                "settlement_ask_pct": float(pending["pct"]),
                "ask_quote": pending.get("pct_quote"),
                "ask_verified": True,
            }
        )
    return out
