"""Table-driven policy tests — one coverage path per PLAN §6.2 rule."""

from __future__ import annotations

from datetime import date

from app.adapter.engine_adapter import Affordability
from app.agent.nlu_types import TurnAnalysis
from app.agent.policy import (
    Intent,
    NegotiationState,
    Phase,
    ask_pct_to_bp,
    decide,
    draft_agreement,
    next_counter,
)
from app.config import Settings
from app.domain.belief import BeliefState, TermStatus
from app.domain.facts import Fact
from app.domain.scenario import load_scenario
from app.store.audit import AuditLog


def _belief(**known: object) -> BeliefState:
    b = BeliefState(load_scenario("fixtures/demo").client)
    for field, value in known.items():
        b.observe(field, value, "q", 1, verified=True, hedged=False)
    return b


def _neg(**kwargs: object) -> NegotiationState:
    return NegotiationState(**kwargs)  # type: ignore[arg-type]


def _afford(
    max_bp: int | None,
    feasible: list[int] | None = None,
) -> Affordability:
    if max_bp is None:
        return Affordability(max_bp=None, feasible_bps=[], curve=tuple([False] * 100))
    bps = feasible if feasible is not None else list(range(100, max_bp + 1, 100))
    curve = tuple(bp in set(bps) for bp in range(100, 10001, 100))
    return Affordability(max_bp=max_bp, feasible_bps=bps, curve=curve)


_SETTINGS = Settings(
    hostility_threshold=0.8,
    max_turns=24,
    max_counters=4,
    anchor_ratio=0.7,
    concession_factor=0.5,
)


def test_ask_pct_to_bp() -> None:
    assert ask_pct_to_bp(45.0) == 4500
    assert ask_pct_to_bp(45.5) == 4550


def test_rule1_max_turns() -> None:
    action = decide(
        _belief(max_payments=6, min_payment_cents=10000, payment_structure="even"),
        _neg(turn_idx=25, ask_bp=5000, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="other"),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.NO_DEAL_WRAP
    assert action.reason == "max_turns"


def test_rule2_hostility_escalates() -> None:
    action = decide(
        _belief(),
        _neg(turn_idx=1),
        TurnAnalysis(hostility=0.9),
        None,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ESCALATE
    assert action.reason == "hostile"


def test_rule3_private_first_refuse_then_escalate() -> None:
    b = _belief()
    a1 = decide(
        b,
        _neg(turn_idx=1, private_ask_count=0),
        TurnAnalysis(asks_client_private_info=True),
        None,
        settings=_SETTINGS,
    )
    assert a1.intent == Intent.REFUSE_PRIVATE
    a2 = decide(
        b,
        _neg(turn_idx=2, private_ask_count=1),
        TurnAnalysis(asks_client_private_info=True),
        None,
        settings=_SETTINGS,
    )
    assert a2.intent == Intent.ESCALATE
    assert a2.reason == "sensitive_request"


def test_rule4_commitment_first_refuse_then_escalate() -> None:
    b = _belief()
    a1 = decide(
        b,
        _neg(turn_idx=1, commit_demand_count=0),
        TurnAnalysis(demands_commitment=True),
        None,
        settings=_SETTINGS,
    )
    assert a1.intent == Intent.REFUSE_COMMIT
    a2 = decide(
        b,
        _neg(turn_idx=2, commit_demand_count=1),
        TurnAnalysis(demands_commitment=True),
        None,
        settings=_SETTINGS,
    )
    assert a2.intent == Intent.ESCALATE
    assert a2.reason == "commitment_demand"


def test_rule5_contradiction_before_readback_before_ask() -> None:
    b = _belief()
    b.observe("max_payments", 6, "six", 1, verified=True, hedged=False)
    b.observe("max_payments", 8, "eight", 2, verified=True, hedged=False)
    assert b.get("max_payments").status == TermStatus.CONTRADICTED
    # Also plant tentative + missing so order matters.
    b.observe("min_payment_cents", 10000, "about", 1, verified=False, hedged=False)
    assert b.get("min_payment_cents").status == TermStatus.TENTATIVE

    action = decide(
        b,
        _neg(turn_idx=3),
        TurnAnalysis(stance="info"),
        None,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CLARIFY
    assert action.reason == "max_payments"
    assert any(e.kind == "note_clarify" for e in action.effects)


def test_rule5_contradiction_unresolved_escalates() -> None:
    b = _belief()
    b.observe("max_payments", 6, "six", 1, verified=True, hedged=False)
    b.observe("max_payments", 8, "eight", 2, verified=True, hedged=False)
    action = decide(
        b,
        _neg(turn_idx=5, clarify_counts={"max_payments": 2}),
        TurnAnalysis(stance="info"),
        None,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ESCALATE
    assert action.reason == "contradiction_unresolved"


def test_rule6_tentative_readback_before_ask() -> None:
    b = _belief()
    b.observe("max_payments", 6, "about six", 1, verified=False, hedged=False)
    action = decide(
        b,
        _neg(turn_idx=1),
        TurnAnalysis(stance="info"),
        None,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.READ_BACK
    assert action.reason == "max_payments"
    assert any(e.kind == "set_pending_readback" for e in action.effects)


def test_rule7_ask_missing_required_registry_order() -> None:
    b = _belief()  # all required missing
    action = decide(
        b,
        _neg(turn_idx=1),
        TurnAnalysis(stance="info"),
        None,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ASK
    assert action.reason == "max_payments"


def test_rule8_ask_settlement_when_rules_known() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=1, ask_bp=None, phase=Phase.DISCOVERY),
        TurnAnalysis(stance="info"),
        _afford(5000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ASK_SETTLEMENT


def test_rule9_rescue_escalates_without_speaking_amounts() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=2, ask_bp=6000, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="offer", settlement_ask_pct=60.0),
        _afford(None),
        settings=_SETTINGS,
        rescue_within_guardrail=True,
    )
    assert action.intent == Intent.ESCALATE
    assert action.reason == "out_of_guardrail"
    # No money/pct facts — rescue amounts must not be spoken.
    assert action.facts == {}
    joined = " ".join(action.text_slots.values())
    assert "$" not in joined
    assert not any(ch.isdigit() for ch in joined)


def test_rule9_infeasible_no_rescue_no_deal() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=2, ask_bp=6000),
        TurnAnalysis(stance="offer"),
        _afford(None),
        settings=_SETTINGS,
        rescue_within_guardrail=False,
    )
    assert action.intent == Intent.NO_DEAL_WRAP


def test_rule9_confirm_when_ask_feasible() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    facts = {
        "offer_total": Fact(
            id="offer_total", kind="money", value=75_000, visibility="PUBLIC", source="engine"
        ),
        "num_payments": Fact(
            id="num_payments", kind="count", value=6, visibility="PUBLIC", source="engine"
        ),
        "first_payment_date": Fact(
            id="first_payment_date",
            kind="date",
            value=date(2026, 4, 15),
            visibility="PUBLIC",
            source="engine",
        ),
    }
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="offer"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
        confirm_facts=facts,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.next_phase == Phase.CONFIRM


def test_rule9_accept_last_counter_confirms() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(
            turn_idx=4,
            ask_bp=7000,
            counters_offered=[4000, 5000],
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="accept"),
        _afford(6000),
        settings=_SETTINGS,
        confirm_facts={
            "offer_total": Fact(
                id="offer_total",
                kind="money",
                value=50_000,
                visibility="PUBLIC",
                source="engine",
            ),
        },
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE


def test_rule10_confirm_phase_accept_propose_wrap() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=5, ask_bp=4500, phase=Phase.CONFIRM),
        TurnAnalysis(stance="accept"),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.PROPOSE_WRAP
    assert action.next_phase == Phase.WRAP


def test_counters_snap_to_feasible_never_at_or_above_ask_never_decreasing() -> None:
    # Feasible: 1000, 2000, 3000, 4000, 5000. Ask 5500, max 5000.
    feasible = [1000, 2000, 3000, 4000, 5000]
    c0 = next_counter(
        ask_bp=5500,
        max_bp=5000,
        feasible_bps=feasible,
        c_prev=None,
        anchor_ratio=0.7,
        concession_factor=0.5,
    )
    # anchor = largest <= 0.7*5000=3500 → 3000
    assert c0 == 3000
    assert c0 < 5500

    c1 = next_counter(
        ask_bp=5500,
        max_bp=5000,
        feasible_bps=feasible,
        c_prev=c0,
        anchor_ratio=0.7,
        concession_factor=0.5,
    )
    assert c1 in feasible
    assert c1 >= c0
    assert c1 < 5500

    c2 = next_counter(
        ask_bp=5500,
        max_bp=5000,
        feasible_bps=feasible,
        c_prev=c1,
        anchor_ratio=0.7,
        concession_factor=0.5,
    )
    assert c2 >= c1
    assert c2 < 5500
    assert c2 in feasible


def test_counter_via_decide_snaps_and_records_effect() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    feasible = [1000, 2000, 3000, 4000, 5000]
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=7000, counters_offered=[], phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="offer"),
        _afford(5000, feasible),
        settings=_SETTINGS,
        counter_offer_total_cents=40_000,
    )
    assert action.intent == Intent.COUNTER
    bp = action.facts["counter_pct"].value
    assert isinstance(bp, int)
    assert bp in feasible
    assert bp < 7000
    assert any(e.kind == "offer_counter" and e.data["bp"] == bp for e in action.effects)


def test_no_deal_after_max_counters_at_max_bp() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(
            turn_idx=10,
            ask_bp=8000,
            counters_offered=[5000],
            rejects=4,
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="reject"),
        _afford(5000, list(range(100, 5100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.NO_DEAL_WRAP
    assert action.reason == "max_counters"


def test_confirm_reject_asks_assumed_field_not_same_schedule() -> None:
    """PLAN §6.2 rule 10: deny in CONFIRM re-enters discovery — never spam same confirm."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    # first_payment_date stays ASSUMED from BeliefState seed
    assert b.get("first_payment_date").status == TermStatus.ASSUMED
    fpd = b.get("first_payment_date").value
    assert isinstance(fpd, date)
    key = (4500, 6, 10000, "even", fpd.isoformat())
    action = decide(
        b,
        _neg(
            turn_idx=5,
            ask_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=0,
        ),
        TurnAnalysis(stance="reject"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ASK
    assert action.reason == "first_payment_date"
    assert action.intent != Intent.CONFIRM_SCHEDULE
    assert any(e.kind == "inc_confirm_reject" for e in action.effects)
    assert any(
        e.kind == "note_assumed_asked" and e.data.get("field") == "first_payment_date"
        for e in action.effects
    )


def test_confirm_assumed_asked_skips_to_next_or_no_deal() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    fpd = b.get("first_payment_date").value
    assert isinstance(fpd, date)
    key = (4500, 6, 10000, "even", fpd.isoformat())
    # All assumed fields already probed once → no-deal, not infinite ASK.
    asked = {"first_payment_date", "max_segments", "max_token_pays", "min_payment_tiers"}
    action = decide(
        b,
        _neg(
            turn_idx=8,
            ask_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=1,
            assumed_asked=asked,
        ),
        TurnAnalysis(stance="reject"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.NO_DEAL_WRAP
    assert action.reason == "confirm_rejected"


def test_confirm_identical_key_without_reject_stance_soft_retries() -> None:
    """NLU miss on accept: soft re-offer CONFIRM, do not burn ASSUMED fields."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    fpd = b.get("first_payment_date").value
    assert isinstance(fpd, date)
    key = (4500, 6, 10000, "even", fpd.isoformat())
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=1,
        ),
        TurnAnalysis(stance="other"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert any(e.kind == "inc_confirm_reject" for e in action.effects)


def test_confirm_unacked_after_max_soft_retries() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    fpd = b.get("first_payment_date").value
    assert isinstance(fpd, date)
    key = (4500, 6, 10000, "even", fpd.isoformat())
    action = decide(
        b,
        _neg(
            turn_idx=8,
            ask_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=4,
        ),
        TurnAnalysis(stance="other"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.NO_DEAL_WRAP
    assert action.reason == "confirm_unacked"


def test_confirm_reject_no_assumed_no_deal_after_max() -> None:
    b = _belief(
        max_payments=6,
        min_payment_cents=10000,
        payment_structure="even",
        first_payment_date=date(2026, 4, 15),
        max_segments=2,
        max_token_pays=6,
        min_payment_tiers=[],
    )
    # Force all non-required out of ASSUMED
    for name in ("first_payment_date", "max_segments", "max_token_pays", "min_payment_tiers"):
        b.terms[name].status = TermStatus.KNOWN
    key = (4500, 6, 10000, "even", "2026-04-15")
    action = decide(
        b,
        _neg(
            turn_idx=8,
            ask_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=4,
        ),
        TurnAnalysis(stance="reject"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.NO_DEAL_WRAP
    assert action.reason == "confirm_rejected"


def test_confirm_records_key_on_first_offer() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="offer"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
        confirm_facts={
            "offer_total": Fact(
                id="offer_total",
                kind="money",
                value=50_000,
                visibility="PUBLIC",
                source="engine",
            ),
        },
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    rec = [e for e in action.effects if e.kind == "record_confirm"]
    assert len(rec) == 1
    assert rec[0].data["ask_bp"] == 4500


def test_identical_counter_without_reject_stance_counts_toward_cap() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    feasible = list(range(100, 5100, 100))
    action = decide(
        b,
        _neg(
            turn_idx=10,
            ask_bp=8000,
            counters_offered=[5000],
            rejects=3,
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="other"),
        _afford(5000, feasible),
        settings=_SETTINGS,
        counter_offer_total_cents=40_000,
    )
    assert action.intent == Intent.NO_DEAL_WRAP
    assert action.reason == "max_counters"


def test_draft_agreement_pending_and_audited(tmp_path) -> None:
    log = AuditLog(tmp_path / "a.db")
    agr = draft_agreement(
        creditor="NorthPeak",
        bp=4500,
        offer_total=75_000,
        rows=[
            {
                "date": "2026-04-15",
                "creditor_payment_cents": 12500,
                "program_fee_cents": 0,
                "bank_fee_cents": 950,
                "balance_cents": 10000,
            }
        ],
        assumed_fields=["max_segments"],
        audit=log,
        call_id="call-1",
    )
    assert agr.status == "pending_client_approval"
    events = log.for_call("call-1")
    assert events[-1]["type"] == "agreement_drafted"
    log.close()
