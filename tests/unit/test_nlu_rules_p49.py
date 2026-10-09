"""Phase 49: code-side reading rules that fire whatever the model returned.

- Private finance shapes (ledger 44b.1): money held for the client, "the most
  they could pay", SSN digits; public questions about the debt never fire.
- Hostility floor (44b.2): clear insults / threats reach the escalation
  threshold; negated, quoted or hypothetical uses and plain frustration do not.
- Balance statements (46b.3, 44b.4): a model's ``amount_ambiguous`` flag on
  "original balance of $6,000" asks nothing.
- Bare demanded amounts (44b.5): "We would need $600 from the client." asks
  "total or per payment?" even when the model read a total, a minimum or nothing.

The held-out lines for these rules are in ``tests/nlu_corpus_rules_heldout.jsonl``
(committed before the rules); this file holds the unit cases.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.agent.nlu import (
    HOSTILITY_FLOOR,
    VerifiedAnalysis,
    VerifiedTerm,
    balance_statement_amount,
    bare_amount_ask,
    hostility_floor_hit,
    post_verify,
    resolve_amounts,
)
from app.agent.nlu_types import TurnAnalysis
from app.agent.orchestrator import Orchestrator
from app.agent.session import CallSession
from app.domain.actions import Intent
from app.domain.scenario import load_scenario
from app.store.audit import AuditLog
from tests.wsutil import offline_settings

_REF = date(2026, 3, 1)
_BALANCE = 125000


def _private(utterance: str) -> bool:
    return post_verify(
        TurnAnalysis(stance="question"), utterance, ref=_REF
    ).asks_client_private_info


# ----- private finance shapes -----


@pytest.mark.parametrize(
    "utterance",
    [
        # Cerebras P44 misses p05, p11, p14, p18 (shapes, not the corpus lines).
        "How much is in their settlement account at the moment?",
        "What's the most your client could pay if we waited a month?",
        "What's the most the client can pay up front?",
        "Give me the last four of the debtor's social.",
        "What's the escrow balance for this person?",
        "How much have they put into the dedicated account?",
        "Can I get the client's social security number?",
    ],
)
def test_private_finance_shapes_fire(utterance: str) -> None:
    assert _private(utterance)


@pytest.mark.parametrize(
    "utterance",
    [
        "What's the balance owed on this account?",
        "What was the original balance?",
        "Can you read me the account number on the debt?",
        "Has the client made any payments to us this year?",
        "The most we can do is 6 payments.",
        "The most we could take off is ten percent.",
        "We don't need anything from their escrow account, just a percentage.",
        "Our social media team will send the letter.",
    ],
)
def test_public_debt_questions_do_not_fire(utterance: str) -> None:
    assert not _private(utterance)


# ----- hostility floor -----


@pytest.mark.parametrize(
    "utterance",
    [
        "This is a waste of my time.",
        "Shut up and give me a number.",
        "Are you stupid? I said six payments.",
        "I'm going to report your company, you're scamming people.",
        "You clowns never pay anything.",
        "Stop wasting our time.",
    ],
)
def test_hostility_floor_reaches_the_threshold_when_the_model_misses(utterance: str) -> None:
    out = post_verify(TurnAnalysis(stance="other", hostility=0.0), utterance, ref=_REF)
    assert hostility_floor_hit(utterance)
    assert out.hostility == HOSTILITY_FLOOR
    assert out.hostility >= offline_settings().hostility_threshold


@pytest.mark.parametrize(
    "utterance",
    [
        "This is taking a while.",
        "I'm a little frustrated, but let's keep going.",
        "That's not going to work, the number is too low.",
        "I'm not saying you're stupid, I just disagree.",
        "This isn't a waste of time, let's finish it.",
        'Someone yelled "shut up" in the background, sorry.',
        "If this were a scam we would have heard by now.",
        "I'll report back to my supervisor.",
    ],
)
def test_hostility_floor_near_misses(utterance: str) -> None:
    assert not hostility_floor_hit(utterance)
    out = post_verify(TurnAnalysis(stance="other", hostility=0.0), utterance, ref=_REF)
    assert out.hostility == 0.0


# ----- balance statements never get the amount question -----


@pytest.mark.parametrize(
    "utterance",
    [
        "Our records show an original balance of $6,000.",  # n06 (Cerebras flagged it)
        # P46b's balance-statement shapes, now behind the model's nlu_flag.
        "The original balance of $6,000 is split into 3 even payments.",
        "The client owes $6,000, and we need 3 even payments.",
        "The outstanding balance is $6,000.",
    ],
)
def test_nlu_flag_on_a_balance_statement_asks_nothing(utterance: str) -> None:
    v = VerifiedAnalysis(
        stance="info", amount_ambiguous_cents=600000, amount_ambiguous_quote="$6,000"
    )
    out, pending = resolve_amounts(v, utterance, balance_cents=_BALANCE)
    assert pending is None
    assert out.amount_ambiguous_cents is None
    assert balance_statement_amount(600000, utterance)


def test_nlu_flag_still_asks_when_the_balance_sentence_demands() -> None:
    utt = "We need $2,000 to clear the balance."
    v = VerifiedAnalysis(
        stance="offer", amount_ambiguous_cents=200000, amount_ambiguous_quote="$2,000"
    )
    _, pending = resolve_amounts(v, utt, balance_cents=500000)
    assert pending is not None and pending["trigger"] == "nlu_flag"


# ----- bare demanded amounts -----

_AM10 = "We would need $600 from the client."


@pytest.mark.parametrize(
    "analysis",
    [
        VerifiedAnalysis(stance="offer"),  # the model read nothing
        VerifiedAnalysis(stance="offer", settlement_ask_total_cents=60000, ask_total_quote="$600"),
        VerifiedAnalysis(
            stance="info",
            terms=[
                VerifiedTerm(field="min_payment_cents", value=60000, quote="$600", verified=True)
            ],
        ),
    ],
    ids=["nothing", "total", "min_payment"],
)
def test_bare_amount_asks_whatever_the_model_returned(analysis: VerifiedAnalysis) -> None:
    out, pending = resolve_amounts(analysis, _AM10, balance_cents=_BALANCE)
    assert pending is not None
    assert (pending["cents"], pending["trigger"]) == (60000, "bare_amount")
    assert out.settlement_ask_total_cents is None and out.ask_total_bp is None
    assert not [t for t in out.terms if t.field == "min_payment_cents"]


@pytest.mark.parametrize(
    "utterance",
    [_AM10, "We'd want $450 to close this.", "We'd need $800 from them to resolve this."],
)
def test_bare_amount_shapes(utterance: str) -> None:
    assert bare_amount_ask(utterance) is not None


@pytest.mark.parametrize(
    "utterance",
    [
        "We would need $600 in total from the client.",
        "We'd want $450 altogether to close this.",
        "We would need $150 each from the client.",
        "We'd need $150 per payment on this account.",
        "We'd need 3 payments of $500 to close this.",
        "We would need $500 by June 1 to settle this.",
        "We'd want $450 to close this, which is 45%.",
        "The client owes $3,200 on this account.",
        "We'd take the $2,000 you offered to close this.",
        "We'd need more than $600 from the client.",
        "Would you need $600 from the client?",
        "We don't need $600 from the client.",
        "We'd need $150.",  # a short answer to our minimum-payment question
    ],
)
def test_bare_amount_near_misses(utterance: str) -> None:
    assert bare_amount_ask(utterance) is None


def test_bare_amount_not_asked_right_after_the_rep_answered() -> None:
    v = VerifiedAnalysis(stance="offer")
    _, pending = resolve_amounts(v, _AM10, balance_cents=_BALANCE, check_min_payment=False)
    assert pending is None


@pytest.mark.asyncio
async def test_bare_amount_question_end_to_end(tmp_path: Path) -> None:
    session = CallSession(scenario=load_scenario("fixtures/scenarios/easy_deal"))
    orch = Orchestrator(
        session,
        settings=offline_settings(),
        audit=AuditLog(tmp_path / "a.db"),
        auto_ack=True,
    )
    await orch.start()
    u: Any = await orch.on_creditor_text(_AM10, oracle=TurnAnalysis(stance="offer"))
    assert u.action.intent == Intent.CLARIFY
    text = " ".join(t for _, t in u.sentences)
    assert "$600" in text and "total" in text
    assert orch.session.neg.pending_amount_clarify["trigger"] == "bare_amount"
