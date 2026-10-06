"""Seeded scenario generator with ground truth and balanced strata.

Each scenario packs a synthetic ``CallScenario``, hidden true creditor rules,
opening ask / floor, persona, and labels: ``zopa``, ``should_escalate``,
``stratum`` (deal | rescue | no_fix). ``generate(n, seed)`` is deterministic
and resamples until strata are balanced. Does not import ``app.agent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from random import Random
from typing import Literal

from app.adapter.engine_adapter import affordability, evaluate
from app.domain.fields import PaymentStructure
from app.domain.scenario import CallScenario
from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    add_months,
    end_of_month,
)
from sim.personas import PERSONAS, PersonaName

Stratum = Literal["deal", "rescue", "no_fix"]
STRATA: tuple[Stratum, ...] = ("deal", "rescue", "no_fix")

# Fixed future window so date renders stay stable across runs.
_AS_OF = date(2026, 3, 1)
_FIRST_DRAFT = date(2026, 3, 15)

_CREDITOR_NAMES = (
    "NorthPeak Collections",
    "Summit Recovery Group",
    "Cedar Lane Servicing",
    "Ironwood Credit",
)


@dataclass(frozen=True)
class TrueRules:
    """Hidden creditor rules the sim knows and the agent must discover."""

    max_payments: int
    min_payment_cents: int
    payment_structure: PaymentStructure
    first_payment_date: date
    max_segments: int
    max_token_pays: int
    min_payment_tiers: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Scenario:
    """One sim case: public call inputs + hidden negotiation ground truth."""

    id: str
    call: CallScenario
    true_rules: TrueRules
    opening_ask_bp: int
    floor_bp: int
    persona: PersonaName
    true_max_bp: int | None
    feasible_bps: tuple[int, ...]
    zopa: bool
    rescue_within_guardrail: bool
    should_escalate: bool
    stratum: Stratum


def to_creditor_rules(
    rules: TrueRules,
    *,
    program_fee_pct: float,
    bank_fee_cents: int,
) -> CreditorRules:
    """Map ``TrueRules`` + firm fees into an engine ``CreditorRules``."""
    even = rules.payment_structure == "even"
    balloon = rules.payment_structure == "balloon"
    return CreditorRules(
        max_terms=rules.max_payments,
        max_payments=rules.max_payments,
        min_payment_cents=rules.min_payment_cents,
        max_token_pays=rules.max_token_pays,
        min_payment_tiers=list(rules.min_payment_tiers),
        even_pays=even,
        is_ballooning_allowed=balloon,
        max_segments=rules.max_segments,
        bank_fee_cents=bank_fee_cents,
        program_fee_pct=program_fee_pct,
    )


def _build_client(
    rng: Random,
    *,
    draft_cents: int,
    horizon_months: int,
    sda_balance_cents: int,
    with_debit: bool,
) -> Client:
    last_draft = add_months(_FIRST_DRAFT, horizon_months - 1)
    ledger: list[LedgerEntry] = []
    for i in range(horizon_months):
        ledger.append(
            LedgerEntry(
                date=add_months(_FIRST_DRAFT, i),
                amount_cents=draft_cents,
                type="credit",
            )
        )
    if with_debit and horizon_months >= 3:
        # Small fixed debit mid-horizon (optional noise).
        debit = rng.randint(25, 150) * 100
        ledger.append(
            LedgerEntry(
                date=add_months(_FIRST_DRAFT, horizon_months // 2),
                amount_cents=debit,
                type="debit",
            )
        )
        # Keep starting SDA consistent with ledger credits/debits is hard;
        # seed balance separately as the as-of cash (PLAN range).
    return Client(
        draft_amount_cents=draft_cents,
        draft_day=_FIRST_DRAFT.day,
        first_draft_date=_FIRST_DRAFT,
        last_draft_date=last_draft,
        as_of_date=_AS_OF,
        current_balance_cents=sda_balance_cents,
        ledger=ledger,
    )


_TIER_RATE = 0.3


def _sample_tiers(
    key: str, *, max_payments: int, min_pay: int
) -> tuple[tuple[int, int], ...]:
    """0–2 rising ``(from_payment, min_cents)`` floors above ``min_pay``.

    Uses its own ``Random(key)`` so adding tiers does not shift the main
    sampler's draws for the other fields.
    """
    trng = Random(key)
    if max_payments < 3 or trng.random() >= _TIER_RATE:
        return ()
    n = trng.choice((1, 2)) if max_payments >= 5 else 1
    froms = sorted(trng.sample(range(2, max_payments + 1), n))
    mins: list[int] = []
    cur = min_pay
    for _ in froms:
        cur += trng.randint(1, 4) * 2500
        mins.append(cur)
    return tuple(zip(froms, mins, strict=True))


def _sample_raw(
    rng: Random,
    *,
    persona: PersonaName,
    bias: Stratum | None,
) -> tuple[CallScenario, TrueRules, int, int]:
    """Sample client/offer/rules/ask/floor. ``bias`` nudges toward a stratum."""
    if bias == "deal":
        draft_cents = rng.randint(250, 600) * 100
        horizon = rng.randint(10, 18)
        creditor_bal = rng.randint(1000, 4000) * 100
        max_payments = rng.randint(6, 12)
        min_pay = rng.randint(50, 150) * 100
        structure: PaymentStructure = rng.choice(("even", "flexible", "balloon"))
    elif bias == "rescue":
        # Narrow band: infeasible at every % but lump rescue within 65% of offer.
        # Empirically: ~$200–250 draft, 8–10 mo, ~$3k balance, min ≈ draft.
        draft_cents = rng.randint(200, 250) * 100
        horizon = rng.randint(8, 10)
        creditor_bal = rng.randint(2800, 3200) * 100
        max_payments = rng.randint(4, 6)
        min_pay = draft_cents + rng.randint(0, 50) * 100
        structure = "even"
    elif bias == "no_fix":
        # Either floor above max affordable, or infeasible with out-of-guardrail rescue.
        if rng.random() < 0.5:
            draft_cents = rng.randint(150, 200) * 100
            horizon = rng.randint(6, 8)
            creditor_bal = rng.randint(6000, 8000) * 100
            max_payments = rng.randint(2, 4)
            min_pay = rng.randint(350, 600) * 100
            structure = "even"
        else:
            # Feasible band exists but floor sits above max_bp.
            draft_cents = rng.randint(200, 350) * 100
            horizon = rng.randint(8, 12)
            creditor_bal = rng.randint(3500, 5500) * 100
            max_payments = rng.randint(4, 8)
            min_pay = rng.randint(100, 200) * 100
            structure = "even"
    else:
        draft_cents = rng.randint(150, 600) * 100
        horizon = rng.randint(6, 18)
        creditor_bal = rng.randint(1000, 8000) * 100
        max_payments = rng.randint(3, 18)
        min_pay = rng.randint(50, 400) * 100
        structure = rng.choice(("even", "flexible", "balloon"))

    sda = rng.randint(0, 2000) * 100
    with_debit = rng.random() < 0.25
    client = _build_client(
        rng,
        draft_cents=draft_cents,
        horizon_months=horizon,
        sda_balance_cents=sda,
        with_debit=with_debit,
    )
    mult = rng.uniform(1.1, 1.3)
    original = int(round(creditor_bal * mult))
    fee = round(rng.uniform(0.15, 0.25), 4)
    bank = rng.randint(5, 15) * 100
    creditor = rng.choice(_CREDITOR_NAMES)

    fpd = end_of_month(client.first_draft_date)

    max_segments = 2 if structure != "flexible" else rng.randint(2, 4)
    max_token = max_payments
    tiers = (
        _sample_tiers(
            f"tiers:{draft_cents}:{creditor_bal}:{max_payments}:{min_pay}",
            max_payments=max_payments,
            min_pay=min_pay,
        )
        if bias == "deal"
        else ()
    )

    true = TrueRules(
        max_payments=max_payments,
        min_payment_cents=min_pay,
        payment_structure=structure,
        first_payment_date=fpd,
        max_segments=max_segments,
        max_token_pays=max_token,
        min_payment_tiers=tiers,
    )

    floor_bp = rng.randint(30, 60) * 100
    opening_ask_bp = rng.randint(max(floor_bp // 100 + 5, 55), 85) * 100
    if opening_ask_bp <= floor_bp:
        opening_ask_bp = min(8500, floor_bp + 500)

    call = CallScenario(
        id="pending",
        client=client,
        creditor=creditor,
        creditor_balance_cents=creditor_bal,
        original_balance_cents=original,
        program_fee_pct=fee,
        bank_fee_cents=bank,
    )
    _ = persona  # reserved for future persona-conditioned sampling
    return call, true, opening_ask_bp, floor_bp


def _classify(
    call: CallScenario,
    true: TrueRules,
    opening_ask_bp: int,
    floor_bp: int,
    persona: PersonaName,
) -> tuple[int | None, tuple[int, ...], bool, bool, bool, Stratum]:
    """Compute ground truth labels for a sampled case."""
    rules = to_creditor_rules(
        true,
        program_fee_pct=call.program_fee_pct,
        bank_fee_cents=call.bank_fee_cents,
    )
    aff = affordability(call, rules, true.first_payment_date)
    true_max = aff.max_bp
    feasible = tuple(aff.feasible_bps)
    zopa = (
        true_max is not None
        and floor_bp <= true_max
        and floor_bp in aff.feasible_bps
    )

    # Rescue only meaningful when nothing is affordable at any settlement %.
    rescue_ok = False
    if true_max is None:
        summary = evaluate(call, rules, opening_ask_bp, true.first_payment_date)
        if summary.additional_funds is not None:
            af = summary.additional_funds
            rescue_ok = bool(
                af.lump_sum.within_guardrail or af.monthly_increment.within_guardrail
            )

    if zopa:
        stratum: Stratum = "deal"
        rescue_ok = False
    elif true_max is None and rescue_ok:
        stratum = "rescue"
    else:
        stratum = "no_fix"
        if true_max is not None:
            rescue_ok = False

    should_escalate = (stratum == "rescue") or (persona == "pressuring")
    return true_max, feasible, zopa, rescue_ok, should_escalate, stratum


def _try_one(
    rng: Random,
    *,
    persona: PersonaName,
    want: Stratum,
    max_tries: int = 200,
) -> Scenario | None:
    for _ in range(max_tries):
        call, true, opening, floor = _sample_raw(rng, persona=persona, bias=want)
        true_max, feasible, zopa, rescue_ok, should_esc, stratum = _classify(
            call, true, opening, floor, persona
        )

        # Shape no_fix when we have a feasible band: push floor above max_bp,
        # keeping opening_ask > floor (PLAN invariant).
        if want == "no_fix" and true_max is not None:
            # Need true_max + 500 < opening <= 8500 and floor > true_max.
            if true_max + 1000 > 8500:
                continue
            floor = true_max + 500
            opening = min(8500, floor + 500)
            if opening <= floor:
                continue
            true_max, feasible, zopa, rescue_ok, should_esc, stratum = _classify(
                call, true, opening, floor, persona
            )

        if stratum != want:
            continue
        # For deal: put opening ask on a feasible grid point so flexible
        # personas can confirm without a long counter ladder.
        if stratum == "deal" and feasible:
            floor = feasible[max(0, len(feasible) // 5)]
            # Prefer an ask the client can afford (instant CONFIRM path).
            opening = feasible[min(len(feasible) - 1, (len(feasible) * 2) // 3)]
            if opening <= floor:
                opening = (
                    min(feasible[-1], floor + 500)
                    if floor + 500 <= feasible[-1]
                    else feasible[-1]
                )
            true_max, feasible, zopa, rescue_ok, should_esc, stratum = _classify(
                call, true, opening, floor, persona
            )
            if stratum != "deal":
                continue
        return Scenario(
            id="pending",
            call=call,
            true_rules=true,
            opening_ask_bp=opening,
            floor_bp=floor,
            persona=persona,
            true_max_bp=true_max,
            feasible_bps=feasible,
            zopa=zopa,
            rescue_within_guardrail=rescue_ok,
            should_escalate=should_esc,
            stratum=stratum,
        )
    return None


def scenario_from_truth(
    call: CallScenario,
    true_rules: TrueRules,
    *,
    opening_ask_bp: int,
    floor_bp: int,
    persona: PersonaName,
) -> Scenario:
    """Wrap a fixed ``CallScenario`` + hidden rules as a sim ``Scenario``.

    Labels (``zopa``, ``stratum``, ``should_escalate``) come from the engine
    exactly as for generated cases. Used by ``app.autoplay`` for the curated
    demo fixtures, whose hidden rules live in ``sim.json``.
    """
    true_max, feasible, zopa, rescue_ok, should_esc, stratum = _classify(
        call, true_rules, opening_ask_bp, floor_bp, persona
    )
    return Scenario(
        id=call.id,
        call=call,
        true_rules=true_rules,
        opening_ask_bp=opening_ask_bp,
        floor_bp=floor_bp,
        persona=persona,
        true_max_bp=true_max,
        feasible_bps=feasible,
        zopa=zopa,
        rescue_within_guardrail=rescue_ok,
        should_escalate=should_esc,
        stratum=stratum,
    )


def generate_one(persona: PersonaName, stratum: Stratum, seed: int) -> Scenario:
    """Sample a single scenario for a persona/stratum pair (deterministic)."""
    rng = Random(seed)
    sc = _try_one(rng, persona=persona, want=stratum, max_tries=200)
    if sc is None:
        raise RuntimeError(
            f"failed to sample persona={persona!r} stratum={stratum!r} seed={seed}"
        )
    sid = f"s{seed:04d}_{stratum}_{persona}"
    call = CallScenario(
        id=sid,
        client=sc.call.client,
        creditor=sc.call.creditor,
        creditor_balance_cents=sc.call.creditor_balance_cents,
        original_balance_cents=sc.call.original_balance_cents,
        program_fee_pct=sc.call.program_fee_pct,
        bank_fee_cents=sc.call.bank_fee_cents,
    )
    return Scenario(
        id=sid,
        call=call,
        true_rules=sc.true_rules,
        opening_ask_bp=sc.opening_ask_bp,
        floor_bp=sc.floor_bp,
        persona=sc.persona,
        true_max_bp=sc.true_max_bp,
        feasible_bps=sc.feasible_bps,
        zopa=sc.zopa,
        rescue_within_guardrail=sc.rescue_within_guardrail,
        should_escalate=sc.should_escalate,
        stratum=sc.stratum,
    )


def generate(n: int, seed: int) -> list[Scenario]:
    """Build ``n`` scenarios deterministically for ``seed``, strata balanced.

    Allocation: fill strata in order; personas cycle. Each slot uses an
    independent sub-seed from the master RNG so earlier strata cannot starve
    later ones of entropy.
    """
    if n <= 0:
        return []
    rng = Random(seed)
    targets = balanced_quota(n)

    out: list[Scenario] = []
    counts = {s: 0 for s in STRATA}
    slot = 0
    for stratum in STRATA:
        need = targets[stratum]
        for _ in range(need):
            persona = PERSONAS[slot % len(PERSONAS)]
            slot += 1
            sub_seed = rng.randint(0, 2**31 - 1)
            sc = generate_one(persona, stratum, seed=sub_seed)
            # Re-id under the master seed index for stable listing.
            sid = f"s{seed:04d}_{len(out):03d}_{stratum}_{persona}"
            call = CallScenario(
                id=sid,
                client=sc.call.client,
                creditor=sc.call.creditor,
                creditor_balance_cents=sc.call.creditor_balance_cents,
                original_balance_cents=sc.call.original_balance_cents,
                program_fee_pct=sc.call.program_fee_pct,
                bank_fee_cents=sc.call.bank_fee_cents,
            )
            sc = Scenario(
                id=sid,
                call=call,
                true_rules=sc.true_rules,
                opening_ask_bp=sc.opening_ask_bp,
                floor_bp=sc.floor_bp,
                persona=sc.persona,
                true_max_bp=sc.true_max_bp,
                feasible_bps=sc.feasible_bps,
                zopa=sc.zopa,
                rescue_within_guardrail=sc.rescue_within_guardrail,
                should_escalate=sc.should_escalate,
                stratum=sc.stratum,
            )
            out.append(sc)
            counts[stratum] += 1

    for s, t in targets.items():
        if t > 0 and counts[s] == 0:
            raise RuntimeError(f"stratum {s} missing after generate({n}, {seed})")
    return out


def stratum_counts(scenarios: list[Scenario]) -> dict[Stratum, int]:
    """Count scenarios per stratum (test helper)."""
    out: dict[Stratum, int] = {s: 0 for s in STRATA}
    for sc in scenarios:
        out[sc.stratum] += 1
    return out


def balanced_quota(n: int) -> dict[Stratum, int]:
    """Expected per-stratum counts for ``generate(n, …)``."""
    base, rem = divmod(n, len(STRATA))
    targets = {s: base for s in STRATA}
    for i, s in enumerate(STRATA):
        if i < rem:
            targets[s] += 1
    return targets
