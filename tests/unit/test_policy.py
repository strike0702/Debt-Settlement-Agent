"""Table-driven policy tests — one coverage path per PLAN §6.2 rule."""

from __future__ import annotations

from datetime import date

from app.adapter.engine_adapter import Affordability
from app.agent import policy as policy_mod
from app.agent.nlg import render_action
from app.agent.nlu_types import ExtractedTerm, TurnAnalysis
from app.agent.policy import (
    Intent,
    NegotiationState,
    Phase,
    anchor_bp,
    ask_pct_to_bp,
    decide,
    draft_agreement,
    rep_turn_progress,
    step_bp,
)
from app.config import Settings
from app.domain.belief import BeliefState, TermStatus
from app.domain.facts import Fact
from app.domain.scenario import load_scenario
from app.store.audit import AuditLog


def _key(belief: BeliefState, ask_bp: int) -> tuple:
    return policy_mod._confirm_key(belief, ask_bp)


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
    assert ask_pct_to_bp(45.125) == 4513


def test_rule1_max_turns() -> None:
    action = decide(
        _belief(max_payments=6, min_payment_cents=10000, payment_structure="even"),
        _neg(turn_idx=25, ask_bp=5000, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="other"),
        _afford(6000),
        settings=_SETTINGS,
    )
    # Deal-or-handoff (Phase 45): the turn cap is a handoff, not a no-deal end.
    assert action.intent == Intent.ESCALATE
    assert action.reason == "max_turns"
    assert action.next_phase == Phase.ESCALATE


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
    assert action.intent == Intent.ESCALATE
    assert action.reason == "infeasible"
    assert "escalate_reason" in action.text_slots


def test_rule9_infeasible_with_alt_date_counters_terms() -> None:
    b = _belief(max_payments=8, min_payment_cents=10000, payment_structure="even")
    alt = date(2026, 9, 30)
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500),
        TurnAnalysis(stance="info"),
        _afford(None),
        settings=_SETTINGS,
        rescue_within_guardrail=False,
        term_alt=("first_payment_date", alt),
    )
    assert action.intent == Intent.COUNTER_TERMS
    assert action.facts["alt_first_payment_date"].value == alt
    # Already offered this field → hand off when no further alt.
    action2 = decide(
        b,
        _neg(
            turn_idx=4,
            ask_bp=4500,
            terms_countered=[f"first_payment_date:{alt.isoformat()}"],
        ),
        TurnAnalysis(stance="reject"),
        _afford(None),
        settings=_SETTINGS,
        rescue_within_guardrail=False,
        term_alt=("first_payment_date", alt),
    )
    assert action2.intent == Intent.ESCALATE
    assert action2.reason == "infeasible"


def test_rule9_alt_date_beats_rescue_escalate() -> None:
    """A workable start date wins over rescue-within-guardrail escalate."""
    b = _belief(max_payments=5, min_payment_cents=10000, payment_structure="balloon")
    alt = date(2027, 1, 31)
    action = decide(
        b,
        _neg(turn_idx=5, ask_bp=8000, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="offer", settlement_ask_pct=80.0),
        _afford(None),
        settings=_SETTINGS,
        rescue_within_guardrail=True,
        term_alt=("first_payment_date", alt),
    )
    assert action.intent == Intent.COUNTER_TERMS
    assert action.reason == "alt_first_payment_date"
    assert action.facts["alt_first_payment_date"].value == alt


def test_rule9_alt_min_payment_beats_rescue() -> None:
    b = _belief(max_payments=5, min_payment_cents=10000, payment_structure="balloon")
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=8000,
            phase=Phase.NEGOTIATE,
            terms_countered=["first_payment_date:2027-01-31"],
        ),
        TurnAnalysis(stance="reject"),
        _afford(None),
        settings=_SETTINGS,
        rescue_within_guardrail=True,
        term_alt=("min_payment_cents", 5000),
    )
    assert action.intent == Intent.COUNTER_TERMS
    assert action.reason == "alt_min_payment_cents"
    assert action.facts["alt_min_payment_cents"].value == 5000


def test_ask_above_ceiling_prefers_further_term_alt() -> None:
    """Tiny unlocked ceiling + better min alt → COUNTER_TERMS, not 8% loop."""
    b = _belief(max_payments=5, min_payment_cents=5000, payment_structure="balloon")
    action = decide(
        b,
        _neg(
            turn_idx=8,
            ask_bp=5000,
            counters_offered=[800],
            phase=Phase.NEGOTIATE,
            terms_countered=[
                "first_payment_date:2027-01-31",
                "min_payment_cents:5000",
            ],
        ),
        TurnAnalysis(stance="reject", settlement_ask_pct=50.0),
        _afford(800, [800]),
        settings=_SETTINGS,
        term_alt=("min_payment_cents", 3000),
        counter_offer_total_cents=5600,
    )
    assert action.intent == Intent.COUNTER_TERMS
    assert action.reason == "alt_min_payment_cents"
    assert action.facts["alt_min_payment_cents"].value == 3000


def test_rule9_reject_terms_cascades_to_next_alt() -> None:
    """Reject pending FPD alt → clear pending and offer min payment, not NO_DEAL."""
    b = _belief(max_payments=5, min_payment_cents=10000, payment_structure="balloon")
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=8000,
            phase=Phase.NEGOTIATE,
            terms_countered=["first_payment_date:2027-01-31"],
            pending_terms_alt={
                "field": "first_payment_date",
                "value": "2027-01-31",
            },
        ),
        TurnAnalysis(stance="reject"),
        _afford(None),
        settings=_SETTINGS,
        rescue_within_guardrail=True,
        term_alt=("min_payment_cents", 4000),
    )
    assert action.intent == Intent.COUNTER_TERMS
    assert action.reason == "alt_min_payment_cents"
    assert any(e.kind == "clear_pending_terms_alt" for e in action.effects)


def test_rule9_alt_max_payments_beats_rescue() -> None:
    b = _belief(max_payments=4, min_payment_cents=10000, payment_structure="balloon")
    action = decide(
        b,
        _neg(
            turn_idx=7,
            ask_bp=8000,
            phase=Phase.NEGOTIATE,
            terms_countered=[
                "first_payment_date:2027-01-31",
                "min_payment_cents:5000",
            ],
        ),
        TurnAnalysis(stance="reject"),
        _afford(None),
        settings=_SETTINGS,
        rescue_within_guardrail=True,
        term_alt=("max_payments", 8),
    )
    assert action.intent == Intent.COUNTER_TERMS
    assert action.reason == "alt_max_payments"
    assert action.facts["alt_max_payments"].value == 8


def test_readback_uses_field_label_not_raw_name() -> None:
    b = _belief(max_payments=8, min_payment_cents=10000, payment_structure="even")
    b.observe(
        "first_payment_date",
        date(2026, 10, 31),
        "31st Oct",
        2,
        verified=False,
        hedged=False,
    )
    action = decide(
        b,
        _neg(turn_idx=2, ask_bp=4500),
        TurnAnalysis(stance="info"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.READ_BACK
    assert action.text_slots.get("field_label") == "initial payment date"
    assert "first_payment_date" not in action.text_slots.get("field_label", "")
    assert "field" not in action.text_slots


def test_readback_empty_tiers_speaks_natural_copy() -> None:
    b = _belief(max_payments=8, min_payment_cents=10000, payment_structure="even")
    b.observe("min_payment_tiers", [], "no tiered minimums", 2, verified=False, hedged=False)
    action = decide(
        b,
        _neg(turn_idx=2, ask_bp=4500),
        TurnAnalysis(stance="info"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.READ_BACK
    text = " ".join(render_action(action, date(2026, 3, 1)))
    assert "[]" not in text
    assert "no special payment tiers" in text


def _tentative_tiers_in_confirm() -> tuple[BeliefState, tuple]:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    b.observe("min_payment_tiers", [], "no tiered minimums", 4, verified=False, hedged=False)
    return b, _key(b, 4800)


def test_readback_during_confirm_keeps_phase_and_notes_accept() -> None:
    """Accept of the CONFIRM preempted by a tiers READ_BACK is remembered."""
    b, key = _tentative_tiers_in_confirm()
    action = decide(
        b,
        _neg(
            turn_idx=4,
            ask_bp=6900,
            counters_offered=[4800],
            confirmed_bp=4800,
            last_confirm_key=key,
            phase=Phase.CONFIRM,
        ),
        TurnAnalysis(stance="accept"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.READ_BACK
    assert action.next_phase == Phase.CONFIRM
    noted = [e for e in action.effects if e.kind == "note_confirm_accepted"]
    assert noted and tuple(noted[0].data["key"]) == key


def test_readback_yes_after_accepted_confirm_wraps() -> None:
    """Seed-7 eval: readback yes after 'Agreed' re-countered 58% over an accepted 48%."""
    b, key = _tentative_tiers_in_confirm()
    b.confirm_readback("min_payment_tiers", True)
    action = decide(
        b,
        _neg(
            turn_idx=5,
            ask_bp=6900,
            counters_offered=[4800],
            confirmed_bp=4800,
            last_confirm_key=key,
            accepted_confirm_key=key,
            phase=Phase.CONFIRM,
        ),
        TurnAnalysis(stance="info", readback_response="confirm"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.PROPOSE_WRAP


def test_readback_yes_after_accept_with_revised_terms_reconfirms() -> None:
    """Seed-7 s0007_012: late reveal changed max_segments, then 'Agreed' → re-confirm 48%."""
    b, key = _tentative_tiers_in_confirm()
    b.observe("max_segments", 4, "4", 4, verified=True, hedged=False)
    b.confirm_readback("min_payment_tiers", True)
    action = decide(
        b,
        _neg(
            turn_idx=5,
            ask_bp=6900,
            counters_offered=[4800],
            confirmed_bp=4800,
            last_confirm_key=key,
            accepted_confirm_key=key,
            phase=Phase.CONFIRM,
        ),
        TurnAnalysis(stance="info", readback_response="confirm"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.reason == "terms_revised"
    assert any(e.kind == "record_confirm" and e.data["ask_bp"] == 4800 for e in action.effects)


def test_readback_deny_after_accepted_confirm_does_not_wrap() -> None:
    b, key = _tentative_tiers_in_confirm()
    b.confirm_readback("min_payment_tiers", False)
    action = decide(
        b,
        _neg(
            turn_idx=5,
            ask_bp=6900,
            counters_offered=[4800],
            confirmed_bp=4800,
            last_confirm_key=key,
            accepted_confirm_key=key,
            phase=Phase.CONFIRM,
        ),
        TurnAnalysis(stance="info", readback_response="deny"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent != Intent.PROPOSE_WRAP


def test_rule9_affordable_ask_counters_at_anchor() -> None:
    """Affordable ask no longer confirms immediately — counter at anchor."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="offer"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.COUNTER
    # anchor = largest <= 0.7 * min(4500, 6000) = 3150 → 3100
    assert action.facts["counter_pct"].value == 3100


def test_negotiate_ask_within_offer_confirms() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(
            turn_idx=5,
            ask_bp=4000,
            counters_offered=[3100, 4200],
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="offer", settlement_ask_pct=40.0),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.reason == "ask_within_offer"
    assert action.facts["settlement_pct"].value == 4000


def test_negotiate_rep_firm_confirms_ask() -> None:
    """Firm at or below the line: one final counter halfway, then accept the repeat."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    afford = _afford(6000, list(range(100, 6100, 100)))  # line = 4500
    final = decide(
        b,
        _neg(turn_idx=5, ask_bp=4500, counters_offered=[3100], phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="reject", firm=True),
        afford,
        settings=_SETTINGS,
    )
    assert final.intent == Intent.COUNTER
    assert final.reason == "final_counter"
    assert final.facts["counter_pct"].value == 3800  # halfway 3100 → 4500
    offer = next(e for e in final.effects if e.kind == "offer_counter")
    assert offer.data["final_ask"] == 4500
    repeat = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=4500,
            counters_offered=[3100, 3800],
            final_counter_ask=4500,
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="reject", firm=True),
        afford,
        settings=_SETTINGS,
    )
    assert repeat.intent == Intent.CONFIRM_SCHEDULE
    assert repeat.reason == "rep_firm"
    assert repeat.facts["settlement_pct"].value == 4500


def test_negotiate_firm_first_turn_still_counters() -> None:
    """Firm with no prior counter does not skip negotiation."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="reject", firm=True),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.COUNTER
    assert action.reason == "final_counter"
    assert action.facts["counter_pct"].value < 4500


def test_negotiate_counters_exhausted_confirms() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    neg = dict(
        turn_idx=8,
        counters_offered=[3100, 3800, 4100, 4300],
        counter_turns=[4, 5, 6, 7],
        ask_at_last_counter=4600,
        phase=Phase.NEGOTIATE,
    )
    action = decide(
        b,
        _neg(ask_bp=4500, **neg),
        TurnAnalysis(stance="reject"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.reason == "counters_exhausted"
    above = decide(
        b,
        _neg(ask_bp=5000, **neg),
        TurnAnalysis(stance="reject"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert above.intent == Intent.ESCALATE
    assert above.reason == "max_counters"


def test_negotiate_no_lower_counter_confirms() -> None:
    """Ask is the only feasible point at/under the line → accepting the first number is allowed."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="offer"),
        _afford(6000, [4500]),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.reason == "no_lower_counter"


def test_never_counter_below_confirmed_bp_on_restate() -> None:
    """Restating the same ask in CONFIRM soft-retries, does not re-counter lower."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            counters_offered=[3100],
            confirm_rejects=0,
        ),
        TurnAnalysis(stance="other"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.facts["settlement_pct"].value == 4500


def test_terms_revised_reconfirms_at_confirmed_bp() -> None:
    b = _belief(max_payments=3, min_payment_cents=10000, payment_structure="even")
    old = _belief(max_payments=8, min_payment_cents=10000, payment_structure="even")
    old_key = _key(old, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=7,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=old_key,
        ),
        TurnAnalysis(stance="offer"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.reason == "terms_revised"
    assert action.facts["settlement_pct"].value == 4500


def test_accept_with_changed_key_does_not_wrap() -> None:
    b = _belief(max_payments=3, min_payment_cents=10000, payment_structure="even")
    old = _belief(max_payments=8, min_payment_cents=10000, payment_structure="even")
    old_key = _key(old, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=7,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=old_key,
        ),
        TurnAnalysis(stance="accept"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent != Intent.PROPOSE_WRAP
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.reason == "terms_revised"


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
        _afford(7000),  # line 5250
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
    assert action.facts["settlement_pct"].value == 5000


def test_rule10_confirm_phase_accept_propose_wrap() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=5,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
        ),
        TurnAnalysis(stance="accept"),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.PROPOSE_WRAP
    assert action.next_phase == Phase.WRAP


def test_confirm_accept_with_corrected_pct_does_not_wrap() -> None:
    """'we agreed at 71%' after a wrong 63% confirm must re-confirm, not wrap."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    key = _key(b, 6300)
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=8000,
            confirmed_bp=6300,
            counters_offered=[6300, 7100],
            phase=Phase.CONFIRM,
            last_confirm_key=key,
        ),
        TurnAnalysis(stance="accept", settlement_ask_pct=71.0, ask_quote="71%"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.facts["settlement_pct"].value == 7100
    assert action.intent != Intent.PROPOSE_WRAP


def test_term_alt_accept_does_not_lock_stale_low_counter() -> None:
    """Yes to a term change must not confirm 8% when the ask is still 60%: re-anchor."""
    b = _belief(max_payments=5, min_payment_cents=5000, payment_structure="balloon")
    feasible = list(range(100, 7800, 100))
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=6000,
            counters_offered=[800],
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="accept"),
        _afford(7700, feasible),
        settings=_SETTINGS,
        accepted_term_alt=True,
    )
    assert action.intent == Intent.COUNTER
    bp = action.facts["counter_pct"].value
    assert isinstance(bp, int)
    assert 800 < bp < 6000


def test_low_confirm_reject_reopens_ladder_when_ask_affordable() -> None:
    """Rejecting an 8% confirm with a 60% ask reopens the ladder: hold once, then climb."""
    b = _belief(max_payments=5, min_payment_cents=5000, payment_structure="balloon")
    key = _key(b, 800)
    feasible = list(range(100, 7800, 100))
    base = dict(
        turn_idx=8,
        ask_bp=800,
        confirmed_bp=800,
        counters_offered=[800],
        phase=Phase.CONFIRM,
        last_confirm_key=key,
    )
    action = decide(
        b,
        _neg(**base),
        TurnAnalysis(stance="reject", settlement_ask_pct=60.0, ask_quote="sixty percent"),
        _afford(7700, feasible),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.COUNTER
    assert action.reason == "hold"
    climb = decide(
        b,
        _neg(**{**base, "hold_stage": 1, "ask_at_last_counter": 6000}),
        TurnAnalysis(stance="reject", settlement_ask_pct=60.0, ask_quote="sixty percent"),
        _afford(7700, feasible),
        settings=_SETTINGS,
    )
    bp = climb.facts["counter_pct"].value
    assert isinstance(bp, int)
    assert 800 < bp < 6000


def test_low_confirm_other_stance_does_not_repeat_eight_percent() -> None:
    """A non-reject line after a too-low confirm holds once, then climbs (no 8% loop)."""
    b = _belief(max_payments=5, min_payment_cents=5000, payment_structure="balloon")
    key = _key(b, 800)
    feasible = list(range(100, 7800, 100))
    action = decide(
        b,
        _neg(
            turn_idx=8,
            ask_bp=6000,
            confirmed_bp=800,
            counters_offered=[800],
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=1,
            hold_stage=1,
            ask_at_last_counter=6000,
        ),
        TurnAnalysis(stance="other"),
        _afford(7700, feasible),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.COUNTER
    bp = action.facts["counter_pct"].value
    assert isinstance(bp, int)
    assert bp > 800


def test_accept_confirms_last_counter() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(
            turn_idx=4,
            ask_bp=8000,
            counters_offered=[6300, 7100],
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="accept"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CONFIRM_SCHEDULE
    assert action.facts["settlement_pct"].value == 7100


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
    """Our best offer at the line is on the table, the rep holds above it → hand off."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(
            turn_idx=10,
            ask_bp=8000,
            counters_offered=[3700],
            ask_at_last_counter=8000,
            hold_stage=1,
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="reject"),
        _afford(5000, list(range(100, 5100, 100))),  # line 3750 → best legal 3700
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ESCALATE
    assert action.reason == "above_accept_line"
    capped = decide(
        b,
        _neg(
            turn_idx=10,
            ask_bp=8000,
            counters_offered=[2600, 3700],
            counter_turns=[3, 4, 5, 6],
            ask_at_last_counter=8000,
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="other"),
        _afford(5000, list(range(100, 5100, 100))),
        settings=_SETTINGS,
    )
    assert (capped.intent, capped.reason) == (Intent.ESCALATE, "max_counters")


def test_confirm_reject_asks_assumed_field_not_same_schedule() -> None:
    """PLAN §6.2 rule 10: deny in CONFIRM re-enters discovery — never spam same confirm."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    # first_payment_date stays ASSUMED from BeliefState seed
    assert b.get("first_payment_date").status == TermStatus.ASSUMED
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=5,
            ask_bp=4500,
            confirmed_bp=4500,
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
    key = _key(b, 4500)
    # All assumed fields already probed once → hand off, not infinite ASK.
    asked = {"first_payment_date", "max_segments", "max_token_pays", "min_payment_tiers"}
    action = decide(
        b,
        _neg(
            turn_idx=8,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=1,
            assumed_asked=asked,
        ),
        TurnAnalysis(stance="reject"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ESCALATE
    assert action.reason == "confirm_rejected"


def test_confirm_identical_key_accept_wraps_not_soft_retry() -> None:
    """Accept on an identical schedule wraps — never a second CONFIRM_SCHEDULE."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=0,
        ),
        TurnAnalysis(stance="accept"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.PROPOSE_WRAP
    assert action.reason == "confirmed"


def test_confirm_identical_key_without_reject_stance_soft_retries() -> None:
    """NLU miss on accept: soft re-offer CONFIRM, do not burn ASSUMED fields."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=4500,
            confirmed_bp=4500,
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
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=8,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=4,
        ),
        TurnAnalysis(stance="other"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ESCALATE
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
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=8,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=key,
            confirm_rejects=4,
        ),
        TurnAnalysis(stance="reject"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ESCALATE
    assert action.reason == "confirm_rejected"


def test_max_segments_change_invalidates_confirm_key() -> None:
    """ASSUMED field change must force a fresh CONFIRM, not soft-retry."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    old_key = _key(b, 4500)
    b.observe("max_segments", 3, "three segments", 2, verified=True, hedged=False)
    new_key = _key(b, 4500)
    assert old_key != new_key
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.CONFIRM,
            last_confirm_key=old_key,
            confirm_rejects=1,
        ),
        TurnAnalysis(stance="other"),
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
    assert action.reason == "terms_revised"
    # Fresh confirm must not burn soft-retry counter.
    assert not any(e.kind == "inc_confirm_reject" for e in action.effects)


def test_readback_response_does_not_wrap_in_confirm() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=5, ask_bp=4500, phase=Phase.CONFIRM),
        TurnAnalysis(stance="other", readback_response="confirm"),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent != Intent.PROPOSE_WRAP


def test_wants_to_end_no_deal() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="other", wants_to_end=True),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ESCALATE
    assert action.reason == "wants_to_end"
    spoken = " ".join(render_action(action, date(2026, 3, 1)))
    assert "specialist from our side will follow up" in spoken


def test_thanks_after_confirm_closes_as_deal() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    neg = _neg(turn_idx=5, ask_bp=4500, phase=Phase.CONFIRM)
    neg.last_confirm_key = ("x",)
    action = decide(
        b,
        neg,
        TurnAnalysis(stance="other", wants_to_end=True),
        _afford(6000),
        settings=_SETTINGS,
        confirm_facts={
            "offer_total": Fact(
                id="offer_total",
                kind="money",
                value=50_000,
                visibility="PUBLIC",
                source="engine",
            )
        },
    )
    assert action.intent == Intent.CLOSE
    assert action.reason == "thanks_accept"
    assert action.next_phase == Phase.END


def test_asks_for_schedule_after_confirm() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    neg = _neg(turn_idx=5, ask_bp=4500, phase=Phase.CONFIRM)
    neg.last_confirm_key = ("x",)
    action = decide(
        b,
        neg,
        TurnAnalysis(stance="question", asks_for_schedule=True),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.SPEAK_SCHEDULE
    assert action.reason == "schedule_detail"


def test_post_wrap_thanks_closes() -> None:
    """After PROPOSE_WRAP, further turns thank and end — never re-confirm."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=6, ask_bp=4500, phase=Phase.WRAP),
        TurnAnalysis(stance="other", wants_to_end=True),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CLOSE
    assert action.next_phase == Phase.END
    assert action.reason == "post_wrap"


def test_post_wrap_any_followup_closes() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=6, ask_bp=4500, phase=Phase.WRAP),
        TurnAnalysis(stance="info"),
        _afford(6000, list(range(100, 6100, 100))),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.CLOSE
    assert action.intent != Intent.CONFIRM_SCHEDULE


def test_post_wrap_renegotiate_new_ask() -> None:
    """New settlement ask after WRAP reopens negotiation (does not CLOSE)."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.WRAP,
            last_confirm_key=key,
        ),
        TurnAnalysis(stance="offer", settlement_ask_pct=70.0, ask_quote="seventy"),
        _afford(8000),
        settings=_SETTINGS,
    )
    assert action.intent != Intent.CLOSE
    assert action.intent != Intent.PROPOSE_WRAP
    assert any(e.kind == "clear_wrap" for e in action.effects)
    assert action.next_phase in (Phase.CONFIRM, Phase.NEGOTIATE)


def test_post_wrap_renegotiate_new_terms() -> None:
    """Revised creditor terms after WRAP reopen; do not CLOSE."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    key = _key(b, 4500)
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=4500,
            confirmed_bp=4500,
            phase=Phase.WRAP,
            last_confirm_key=key,
        ),
        TurnAnalysis(
            stance="info",
            terms=[
                ExtractedTerm(
                    field="max_payments",
                    value=3,
                    quote="three payments",
                    hedged=False,
                )
            ],
        ),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent != Intent.CLOSE
    assert any(e.kind == "clear_wrap" for e in action.effects)


def test_post_wrap_schedule_detail_stays_wrap() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=6, ask_bp=4500, phase=Phase.WRAP, last_confirm_key=("x",)),
        TurnAnalysis(stance="question", asks_for_schedule=True),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.SPEAK_SCHEDULE
    assert action.next_phase == Phase.WRAP
    assert action.reason == "schedule_detail_post_wrap"


def test_confirm_records_key_on_firm_accept() -> None:
    """After our final counter, the repeated firm ask confirms and records the fingerprint."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(
            turn_idx=4,
            ask_bp=4500,
            counters_offered=[3100, 3800],
            final_counter_ask=4500,
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="reject", firm=True),
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
    assert tuple(rec[0].data["key"]) == _key(b, 4500)


def test_ask_known_but_rules_unbuildable_asks_settlement() -> None:
    """Merged branch: afford=None with a known ask behaves like an unknown ask."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500, phase=Phase.DISCOVERY),
        TurnAnalysis(stance="info"),
        None,
        settings=_SETTINGS,
    )
    assert action.intent == Intent.ASK_SETTLEMENT
    assert action.next_phase == Phase.DISCOVERY
    assert action.reason is None


def test_ask_above_ceiling_fpd_already_countered_ladders() -> None:
    """Shared term-alt gate: FPD gets one try on the ceiling path too."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(
            turn_idx=5,
            ask_bp=8000,
            phase=Phase.NEGOTIATE,
            terms_countered=["first_payment_date:2026-11-30"],
        ),
        TurnAnalysis(stance="reject"),
        _afford(5000, list(range(100, 5100, 100))),
        settings=_SETTINGS,
        term_alt=("first_payment_date", date(2026, 12, 31)),
    )
    assert action.intent == Intent.COUNTER


def test_clarify_enum_field_uses_text_slots() -> None:
    """Enum contradictions speak via text slots, not a numeric Fact."""
    b = _belief()
    b.observe("payment_structure", "even", "even", 1, verified=True, hedged=False)
    b.observe("payment_structure", "balloon", "balloon", 2, verified=True, hedged=False)
    action = decide(b, _neg(turn_idx=3), TurnAnalysis(stance="info"), None, settings=_SETTINGS)
    assert action.intent == Intent.CLARIFY
    assert action.facts == {}
    assert action.text_slots["clarify_old"] == "even"
    assert action.text_slots["clarify_new"] == "balloon"


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


def test_negotiate_firm_above_line_hands_off_at_once() -> None:
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    for counters in ([], [3100]):
        action = decide(
            b,
            _neg(turn_idx=5, ask_bp=5000, counters_offered=counters, phase=Phase.NEGOTIATE),
            TurnAnalysis(stance="reject", firm=True),
            _afford(6000, list(range(100, 6100, 100))),  # line 4500 < 5000 ≤ max
            settings=_SETTINGS,
        )
        assert action.intent == Intent.ESCALATE
        assert action.reason == "above_accept_line"


def test_negotiate_first_number_never_accepted_when_a_counter_exists() -> None:
    """Rule C1: even a tiny gap gets one counter (no gap_small accept)."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=3, ask_bp=4500, phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="offer"),
        _afford(6000, [4400, 4500]),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.COUNTER
    assert action.facts["counter_pct"].value == 4400


def test_negotiate_hold_then_two_steps_then_accept_or_hand_off() -> None:
    """Rule C3: rep does not move → hold, quarter step, quarter step, then decide."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    afford = _afford(6000, list(range(100, 6100, 100)))  # line 4500
    base = dict(turn_idx=6, ask_at_last_counter=4500, phase=Phase.NEGOTIATE)
    hold = decide(
        b,
        _neg(ask_bp=4500, counters_offered=[3000], hold_stage=0, **base),
        TurnAnalysis(stance="reject"),
        afford,
        settings=_SETTINGS,
    )
    assert (hold.intent, hold.reason) == (Intent.COUNTER, "hold")
    assert hold.facts["counter_pct"].value == 3000
    assert next(e for e in hold.effects if e.kind == "offer_counter").data["stage"] == 1
    step1 = decide(
        b,
        _neg(ask_bp=4500, counters_offered=[3000], hold_stage=1, **base),
        TurnAnalysis(stance="reject"),
        afford,
        settings=_SETTINGS,
    )
    # quarter of (min(4500, 4500) - 3000) = 375 → 3375 → snaps to 3300
    assert (step1.reason, step1.facts["counter_pct"].value) == ("step", 3300)
    step2 = decide(
        b,
        _neg(ask_bp=4500, counters_offered=[3000, 3300], hold_stage=2, **base),
        TurnAnalysis(stance="reject"),
        afford,
        settings=_SETTINGS,
    )
    # quarter of (4500 - 3300) = 300 → 3600
    assert (step2.reason, step2.facts["counter_pct"].value) == ("step", 3600)
    done = decide(
        b,
        _neg(ask_bp=4500, counters_offered=[3000, 3300, 3600], hold_stage=3, **base),
        TurnAnalysis(stance="reject"),
        afford,
        settings=_SETTINGS,
    )
    assert (done.intent, done.reason) == (Intent.CONFIRM_SCHEDULE, "rep_held")
    above = decide(
        b,
        _neg(
            ask_bp=5000,
            counters_offered=[3000, 3300, 3600],
            hold_stage=3,
            turn_idx=6,
            ask_at_last_counter=5000,
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="reject"),
        afford,
        settings=_SETTINGS,
    )
    assert (above.intent, above.reason) == (Intent.ESCALATE, "above_accept_line")


def test_negotiate_rep_moves_we_concede_half_and_reset() -> None:
    """Rule C4: rep drops 1000 bp → we rise 500 bp; hold stage resets to 0."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(
            turn_idx=6,
            ask_bp=6000,
            counters_offered=[3000],
            ask_at_last_counter=7000,
            hold_stage=2,
            phase=Phase.NEGOTIATE,
        ),
        TurnAnalysis(stance="reject"),
        _afford(8000, list(range(100, 8100, 100))),  # line 6000
        settings=_SETTINGS,
    )
    assert action.intent == Intent.COUNTER
    assert action.facts["counter_pct"].value == 3500
    offer = next(e for e in action.effects if e.kind == "offer_counter")
    assert offer.data["stage"] == 0 and offer.data["ask_bp"] == 6000


def test_counters_never_exceed_the_accept_line() -> None:
    """Rule C2 across ask / ceiling / prior-counter grids."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    for max_bp in (3000, 5000, 8000, 10000):
        line = max_bp * 7500 // 10000
        afford = _afford(max_bp, list(range(100, max_bp + 1, 100)))
        for ask in range(1000, 10001, 900):
            for prior in ([], [1000], [line - line % 100]):
                for firm in (False, True):
                    for stage in (0, 1, 2):
                        action = decide(
                            b,
                            _neg(
                                turn_idx=5,
                                ask_bp=ask,
                                counters_offered=list(prior),
                                ask_at_last_counter=ask,
                                hold_stage=stage,
                                phase=Phase.NEGOTIATE,
                            ),
                            TurnAnalysis(stance="reject", firm=firm),
                            afford,
                            settings=_SETTINGS,
                        )
                        if action.intent == Intent.COUNTER:
                            bp = action.facts["counter_pct"].value
                            assert isinstance(bp, int) and bp <= line and bp < ask
                        if action.intent == Intent.CONFIRM_SCHEDULE:
                            assert action.facts["settlement_pct"].value <= line


def test_anchor_and_step_helpers() -> None:
    legal = [1000, 2000, 3000, 4000]
    # anchor = largest legal ≤ 0.7 × min(5500, 4000) = 2800 → 2000
    assert anchor_bp(ask_bp=5500, line_bp=4000, legal=legal, anchor_ratio=0.7) == 2000
    # Nothing legal below the ask → None.
    assert anchor_bp(ask_bp=1000, line_bp=4000, legal=legal, anchor_ratio=0.7) is None
    # Step: highest legal ≤ raw above prev, else the next legal bp up, never ≥ ceiling.
    assert step_bp(prev=1000, raw=2500, ceiling=5000, legal=legal) == 2000
    assert step_bp(prev=1000, raw=1500, ceiling=5000, legal=legal) == 2000
    assert step_bp(prev=4000, raw=4500, ceiling=5000, legal=legal) is None


def test_accept_with_a_new_number_is_their_ask_not_a_deal() -> None:
    """Pair-17 shape (A/B s0007_006): "even payments to settle the 100% balance" read
    as accept + 100% during discovery must counter, never confirm the first number."""
    b = _belief(max_payments=7, min_payment_cents=9400, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=2, phase=Phase.DISCOVERY),
        TurnAnalysis(stance="accept", settlement_ask_pct=100.0, ask_quote="100%"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert action.intent == Intent.COUNTER
    assert action.facts["counter_pct"].value < 7500
    # Later "proceed with the 10% balance" (accept + 10%) below our counter is
    # their lower ask: confirmed as ask_within_offer, not a blind accept.
    later = decide(
        b,
        _neg(turn_idx=4, ask_bp=10000, counters_offered=[5200], phase=Phase.NEGOTIATE),
        TurnAnalysis(stance="accept", settlement_ask_pct=10.0, ask_quote="10%"),
        _afford(10000),
        settings=_SETTINGS,
    )
    assert later.intent == Intent.CONFIRM_SCHEDULE
    assert later.reason == "ask_within_offer"


def test_same_question_third_time_hands_off() -> None:
    """Loop guard (B): ASK for one field twice already → hand off, not a third ask."""
    b = _belief(max_payments=6)
    first = decide(b, _neg(turn_idx=2), TurnAnalysis(stance="info"), None, settings=_SETTINGS)
    assert first.intent == Intent.ASK
    note = next(e for e in first.effects if e.kind == "note_question")
    key = note.data["key"]
    again = decide(
        b, _neg(turn_idx=3, question_counts={key: 1}), TurnAnalysis(stance="info"), None,
        settings=_SETTINGS,
    )
    assert again.intent == Intent.ASK
    third = decide(
        b, _neg(turn_idx=4, question_counts={key: 2}), TurnAnalysis(stance="info"), None,
        settings=_SETTINGS,
    )
    assert (third.intent, third.reason) == (Intent.ESCALATE, "repeated_question")


def test_no_progress_turns_hand_off() -> None:
    """Loop guard (B): four rep turns in a row with nothing new → hand off."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    action = decide(
        b,
        _neg(turn_idx=8, ask_bp=4500, phase=Phase.NEGOTIATE, no_progress_turns=4),
        TurnAnalysis(stance="other"),
        _afford(6000),
        settings=_SETTINGS,
    )
    assert (action.intent, action.reason) == (Intent.ESCALATE, "no_progress")


def test_rep_turn_progress_definition() -> None:
    neg = _neg(ask_bp=4500)
    assert not rep_turn_progress(TurnAnalysis(stance="other"), neg, [])
    assert not rep_turn_progress(TurnAnalysis(stance="offer", settlement_ask_pct=45.0), neg, [])
    assert rep_turn_progress(TurnAnalysis(stance="offer", settlement_ask_pct=40.0), neg, [])
    assert rep_turn_progress(TurnAnalysis(stance="reject"), neg, [])
    assert rep_turn_progress(TurnAnalysis(stance="info", readback_response="confirm"), neg, [])
    b = _belief()
    change = b.observe("max_payments", 6, "six", 1, verified=True, hedged=False)
    assert rep_turn_progress(TurnAnalysis(stance="info"), neg, [change])
    same = b.observe("max_payments", 6, "six", 2, verified=True, hedged=False)
    if same.old_value == same.new_value and same.old_status == same.new_status:
        assert not rep_turn_progress(TurnAnalysis(stance="info"), neg, [same])


def test_decide_never_emits_no_deal_wrap() -> None:
    """Deal-or-handoff (A): sweep stances / flags / curves; NO_DEAL_WRAP never appears."""
    b = _belief(max_payments=6, min_payment_cents=10000, payment_structure="even")
    analyses = [
        TurnAnalysis(stance=s, firm=f, wants_to_end=w)  # type: ignore[arg-type]
        for s in ("accept", "reject", "other", "offer")
        for f in (False, True)
        for w in (False, True)
    ]
    for afford in (_afford(None), _afford(3000), _afford(8000)):
        for turn in (3, 30):
            for a in analyses:
                action = decide(
                    b,
                    _neg(turn_idx=turn, ask_bp=7000, counters_offered=[2000],
                         phase=Phase.NEGOTIATE),
                    a,
                    afford,
                    settings=_SETTINGS,
                )
                assert action.intent != Intent.NO_DEAL_WRAP
