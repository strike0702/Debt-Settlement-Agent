"""Brute-force reference for small engine instances: every legal vector, no generator.

Given a small case (S ≤ ~400 cents, k_max ≤ 6, M ≤ 8), ``solve`` enumerates every
creditor payment vector that is legal under the take-home's binding rules 1–9
(count/placement, exact sum, non-decreasing, floors with the token-pay count, bank
fee on payment dates, fee timing, even pays, balloon, segments), simulates each with
its own ledger replay and the suffix-min fee placement, and returns:

- ``feasible`` and the lexicographically best cumulative-fee vector Φ* (plus the
  first vector achieving it in (k ascending, then lexicographic) order, and its
  schedule rows);
- Part 2 minima (when infeasible): smallest lump ``L`` (credited before every
  event) and smallest uniform draft increment ``X``, each by binary search on the
  amount with an existence check over all legal vectors.

``solve_ranked`` (Phase 59) ranks every feasible legal vector by an objective
(``fee_first``: Φ then fewer payments; ``client_first``: cushion = lowest window balance
minus F, then the smallest largest payment, then Φ, then fewer payments) under a fee
policy (``front_loaded``, or ``proportional``: fee by t_j at most floor(F*P_j/S)). These
keys are written from docs/ENGINE.md, not from ``feasibility/objectives.py``.

Written from the problem statement, deliberately *not* from ``feasibility/shapes.py``,
``scoring.py`` or ``simulate.py``: it shares only the date/data models. The DFS prunes
only on facts that hold for every extension (sum bounds, token count, segment cap
when no balloon is possible, and causality: balances in window j depend only on
p_1..p_j). Interpretation notes: the token rule is a count ("at most T payments equal
the base minimum"); a vector is exempt from ``max_segments`` only when ballooning is
allowed *and* its last payment is strictly above the one before (the repo's reading,
shared with ``app/adapter/validator.py``). Used by ``tests/engine/test_oracle.py``
and ``scripts/engine_gap_report.py``; never by ``app/``.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date

from feasibility.engine import ScheduleRow
from feasibility.models import Client, CreditorRules

NODE_BUDGET = 400_000  # DFS nodes per enumeration before ``OracleTooBig``


class OracleTooBig(Exception):
    """The instance is outside the oracle's enumeration budget."""


@dataclass(frozen=True)
class OracleResult:
    """Exhaustive answer for one small case."""

    feasible: bool
    phi: tuple[int, ...] | None  # Φ*: cumulative fee by t_1..t_M
    best: tuple[int, ...] | None  # first vector achieving Φ* (k ascending, then lex)
    rows: list[ScheduleRow] | None
    n_feasible: int  # feasible vectors seen (branch-and-bound skips dominated ones)
    lump_cents: int | None  # minimal L when infeasible; None if feasible or unfixable
    increment_cents: int | None  # minimal X when infeasible; None if feasible or unfixable
    num_drafts: int = 0
    notes: list[str] = field(default_factory=list)


# --- dates ---------------------------------------------------------------


def _last_day(y: int, m: int) -> int:
    return monthrange(y, m)[1]


def cadence(start: date, horizon: date) -> list[date]:
    """Monthly dates from ``start`` up to ``horizon``; EOM start keeps EOM, else clamp."""
    eom = start.day == _last_day(start.year, start.month)
    out: list[date] = []
    i = 0
    while True:
        total = start.year * 12 + start.month - 1 + i
        y, m = divmod(total, 12)
        m += 1
        day = _last_day(y, m) if eom else min(start.day, _last_day(y, m))
        d = date(y, m, day)
        if d > horizon:
            return out
        out.append(d)
        i += 1


# --- rules 1–4, 7–9 ------------------------------------------------------


def floor_at(rules: CreditorRules, i: int) -> int:
    """Position floor from the base minimum and tiers (token rule is a separate count)."""
    f = rules.min_payment_cents
    for frm, m in rules.min_payment_tiers:
        if i >= frm and m > f:
            f = m
    return f


def is_legal(vec: tuple[int, ...], S: int, k_cap: int, rules: CreditorRules) -> bool:
    """Static legality of a payment vector under rules 1–4 and 7–9."""
    k = len(vec)
    if not 1 <= k <= k_cap or sum(vec) != S:
        return False
    if any(vec[i] > vec[i + 1] for i in range(k - 1)):
        return False
    if any(vec[i - 1] < floor_at(rules, i) for i in range(1, k + 1)):
        return False
    if sum(1 for p in vec if p == rules.min_payment_cents) > rules.max_token_pays:
        return False
    if rules.even_pays:
        q, r = divmod(S, k)
        return vec == tuple([q] * (k - r) + [q + 1] * r)
    if rules.is_ballooning_allowed and k >= 2 and vec[-1] > vec[-2]:
        return True
    return len(set(vec)) <= rules.max_segments


# --- the instance ----------------------------------------------------------


class Instance:
    """One case's ledger arithmetic, with optional lump ``L`` and draft increment ``X``."""

    def __init__(
        self,
        client: Client,
        rules: CreditorRules,
        first_payment_date: date,
        S: int,
        F: int,
    ) -> None:
        self.client = client
        self.rules = rules
        self.S = S
        self.F = F
        self.t = cadence(first_payment_date, client.last_draft_date)
        self.M = len(self.t)
        self.k_cap = min(rules.max_payments, rules.max_terms, self.M)
        future = [e for e in client.ledger if e.date > client.as_of_date]
        self.drafts = sorted(e.date for e in future if e.type == "credit")
        dates = sorted({e.date for e in future} | set(self.t))
        self.dates = dates
        credit = {d: 0 for d in dates}
        debit = {d: 0 for d in dates}
        for e in future:
            (credit if e.type == "credit" else debit)[e.date] += e.amount_cents
        self.credit = credit
        self.debit = debit
        # Committed-only running balance and draft count per date (for pruning).
        bal = client.current_balance_cents
        self.committed: list[int] = []
        self.ndrafts: list[int] = []
        for d in dates:
            bal += credit[d] - debit[d]
            self.committed.append(bal)
            self.ndrafts.append(sum(1 for x in self.drafts if x <= d))
        # Number of cadence dates on or before each date (its window index).
        self.win = [sum(1 for x in self.t if x <= d) for d in dates]

    # Full chronological replay (credits before debits), no program fee.
    def replay(self, vec: tuple[int, ...], L: int = 0, X: int = 0) -> list[int]:
        """End-of-day balances on ``self.dates`` for payments ``vec`` on t_1..t_k."""
        pay = {self.t[i]: vec[i] + self.rules.bank_fee_cents for i in range(len(vec))}
        draft_set: dict[date, int] = {}
        for d in self.drafts:
            draft_set[d] = draft_set.get(d, 0) + 1
        bal = self.client.current_balance_cents + L
        out = []
        for d in self.dates:
            bal += self.credit[d] + X * draft_set.get(d, 0)
            bal -= self.debit[d] + pay.get(d, 0)
            out.append(bal)
        return out

    def windows(self, balances: list[int]) -> list[int]:
        """Lowest balance in each cadence window t_j <= d < t_{j+1} (j = 1..M)."""
        low = [10**18] * (self.M + 1)
        for idx, j in enumerate(self.win):
            if j >= 1:
                low[j] = min(low[j], balances[idx])
        return low[1:]

    def phi(
        self, balances: list[int], vec: tuple[int, ...] | None = None
    ) -> tuple[int, ...] | None:
        """Suffix-min fee placement; None when the vector is infeasible (rules 5, 6, 10).

        With ``vec`` the proportional cap applies: Φ_j <= floor(F * paid by t_j / S).
        """
        if min(balances, default=0) < 0:
            return None
        tail = [10**18] * (self.M + 2)
        for idx in range(len(self.dates) - 1, -1, -1):
            j = self.win[idx]
            if j >= 1:
                tail[j] = min(tail[j], balances[idx])
        for j in range(self.M - 1, 0, -1):
            tail[j] = min(tail[j], tail[j + 1])
        if self.M == 0 or tail[self.M] < self.F:
            return None
        if vec is None:
            return tuple(min(self.F, tail[j]) for j in range(1, self.M + 1))
        out = []
        paid = 0
        for j in range(1, self.M + 1):
            if j <= len(vec):
                paid += vec[j - 1]
            out.append(min(self.F, tail[j], self.F * paid // self.S))
        return tuple(out)

    # Legal vectors, pruned by causality: window j's balances depend on p_1..p_j only.
    def vectors(
        self,
        L: int = 0,
        X: int = 0,
        *,
        prune: bool = True,
        cut: Callable[[list[int], list[int]], bool] | None = None,
    ) -> Iterator[tuple[int, ...]]:
        """Every legal vector (that can still be feasible when ``prune``).

        ``cut(ws, sfx)`` sees the window minima ``w_1..w_j`` fixed by p_1..p_j (after
        payments, before fee) and ``sfx[i]`` = min of windows max(i, k)..M, which only
        depends on k because every length-k vector has paid S + k bank fees by then
        (1-indexed, ``sfx[0]`` unused). It returns True to drop every extension and
        must be monotone (a larger p_j only lowers ``w_j``), so the scan over p_j
        stops there.
        """
        rules = self.rules
        bf = rules.bank_fee_cents
        S = self.S
        nodes = 0
        # Lowest balance in window j (index by j) before creditor payments, with L/X.
        low = [10**18] * (self.M + 1)
        for idx, j in enumerate(self.win):
            b = self.committed[idx] + L + X * self.ndrafts[idx]
            if j == 0:
                if b < 0 and prune:
                    return
            else:
                low[j] = min(low[j], b)

        for k in range(1, self.k_cap + 1):
            # Windows k..M see the whole S plus k bank fees whatever the vector.
            sfx = [0] * (self.M + 2)
            sfx[self.M + 1] = 10**18
            for i in range(self.M, 0, -1):
                w_i = low[i] - S - bf * k if i >= k else 10**18
                sfx[i] = min(w_i, sfx[i + 1])
            for i in range(1, k):
                sfx[i] = sfx[k]
            tail = sfx[k]
            if prune and (tail < 0 or low[self.M] - S - bf * k < self.F):
                continue
            if rules.even_pays:
                q, r = divmod(S, k)
                vec = tuple([q] * (k - r) + [q + 1] * r)
                if is_legal(vec, S, self.k_cap, rules):
                    yield vec
                continue
            vec_l: list[int] = []
            ws: list[int] = []

            def rec(j: int, prefix: int, levels: int, tokens: int) -> Iterator[tuple[int, ...]]:
                nonlocal nodes
                nodes += 1
                if nodes > NODE_BUDGET:
                    raise OracleTooBig
                if j > k:
                    if prefix == S:
                        vec = tuple(vec_l)
                        if is_legal(vec, S, self.k_cap, rules):
                            yield vec
                    return
                prev = vec_l[-1] if vec_l else 0
                lo = max(prev, floor_at(rules, j))
                remaining = k - j + 1
                hi = (S - prefix) // remaining  # later payments are >= this one
                if j == k:
                    lo = max(lo, S - prefix)
                    hi = S - prefix
                for p in range(lo, hi + 1):
                    new_tok = tokens + (p == rules.min_payment_cents)
                    if new_tok > rules.max_token_pays:
                        continue
                    new_lv = levels + (p != prev or not vec_l)
                    if not rules.is_ballooning_allowed and new_lv > rules.max_segments:
                        break  # larger p only adds the same or more levels
                    wj = low[j] - (prefix + p) - bf * j
                    if prune and j < k and wj < 0:
                        break  # larger p only lowers window j further
                    # Later payments are >= p, so P_i >= P_j + (i - j) * p for j < i < k.
                    if prune and any(
                        low[i] - (prefix + p) - (i - j) * p - bf * i < 0 for i in range(j + 1, k)
                    ):
                        break
                    ws.append(wj)
                    if cut is not None and j < k and cut(ws, sfx):
                        ws.pop()
                        break
                    vec_l.append(p)
                    yield from rec(j + 1, prefix + p, new_lv, new_tok)
                    vec_l.pop()
                    ws.pop()

            yield from rec(1, 0, 0, 0)

    def feasible_with(self, L: int = 0, X: int = 0) -> bool:
        """Does any legal vector give a feasible schedule with this lump / increment?"""
        return any(self.phi(self.replay(v, L, X)) is not None for v in self.vectors(L, X))

    def rows(self, vec: tuple[int, ...], phi: tuple[int, ...]) -> list[ScheduleRow]:
        """Schedule rows (payment and/or fee dates) for the validator."""
        balances = dict(zip(self.dates, self.replay(vec)))
        k = len(vec)
        out: list[ScheduleRow] = []
        prev = 0
        for j in range(1, self.M + 1):
            fee = phi[j - 1] - prev
            prev = phi[j - 1]
            p = vec[j - 1] if j <= k else 0
            if p == 0 and fee == 0:
                continue
            out.append(
                ScheduleRow(
                    date=self.t[j - 1],
                    creditor_payment_cents=p,
                    program_fee_cents=fee,
                    bank_fee_cents=self.rules.bank_fee_cents if j <= k else 0,
                    balance_cents=balances[self.t[j - 1]] - phi[j - 1],
                )
            )
        return out


def _min_amount(ok, hi: int) -> int | None:
    """Smallest a in [0, hi] with ok(a), assuming monotone; None if not ok(hi)."""
    if not ok(hi):
        return None
    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def solve(
    client: Client, rules: CreditorRules, first_payment_date: date, S: int, F: int
) -> OracleResult:
    """Exhaustive Part 1 (Φ*) and, when infeasible, Part 2 minima by binary search."""
    inst = Instance(client, rules, first_payment_date, S, F)
    best_phi: tuple[int, ...] | None = None
    best_vec: tuple[int, ...] | None = None
    n = 0

    def cut(ws: list[int], sfx: list[int]) -> bool:
        # Pointwise upper bound U on Φ for every extension of p_1..p_j: windows
        # 1..j are fixed, later windows only shrink (each is <= the k-only tail
        # minimum), and Φ_i = min(F, suffix min from i). Φ <= U pointwise implies
        # Φ <= U lexicographically, so U <= Φ* means no extension can beat Φ*
        # (ties go to the vector found first).
        if best_phi is None:
            return False
        j = len(ws)
        U = [min(F, sfx[i]) for i in range(1, inst.M + 1)]
        m = min(F, sfx[j + 1] if j + 1 <= inst.M else 10**18)
        for i in range(j, 0, -1):
            m = min(m, ws[i - 1])
            U[i - 1] = m
        return tuple(U) <= best_phi

    for vec in inst.vectors(cut=cut):
        phi = inst.phi(inst.replay(vec))
        if phi is None:
            continue
        n += 1
        if best_phi is None or phi > best_phi:
            best_phi, best_vec = phi, vec
    n_drafts = len(inst.drafts)
    if best_phi is not None:
        assert best_vec is not None
        return OracleResult(
            feasible=True,
            phi=best_phi,
            best=best_vec,
            rows=inst.rows(best_vec, best_phi),
            n_feasible=n,
            lump_cents=None,
            increment_cents=None,
            num_drafts=n_drafts,
        )
    debits = sum(inst.debit.values())
    hi = S + F + rules.bank_fee_cents * max(inst.k_cap, 0) + debits
    hi += max(0, -client.current_balance_cents) + 1
    lump = _min_amount(lambda a: inst.feasible_with(L=a), hi)
    incr = _min_amount(lambda a: inst.feasible_with(X=a), hi)
    return OracleResult(
        feasible=False,
        phi=None,
        best=None,
        rows=None,
        n_feasible=0,
        lump_cents=lump,
        increment_cents=incr,
        num_drafts=n_drafts,
    )


def phi_of(
    client: Client,
    rules: CreditorRules,
    first_payment_date: date,
    S: int,
    F: int,
    vec: tuple[int, ...],
) -> tuple[int, ...] | None:
    """Oracle Φ for one given vector (None if illegal or infeasible)."""
    inst = Instance(client, rules, first_payment_date, S, F)
    if not is_legal(vec, S, inst.k_cap, rules):
        return None
    return inst.phi(inst.replay(vec))


@dataclass(frozen=True)
class RankedResult:
    """Best feasible legal vector under one objective and fee policy."""

    feasible: bool
    key: tuple | None  # fee_first: (Φ, -k); client_first: (cushion, -p_max, Φ, -k)
    best: tuple[int, ...] | None  # first vector reaching ``key`` (k ascending, then lex)
    phi: tuple[int, ...] | None
    cushion: int | None
    n_feasible: int


def solve_ranked(
    client: Client,
    rules: CreditorRules,
    first_payment_date: date,
    S: int,
    F: int,
    objective: str = "fee_first",
    fee_policy: str = "front_loaded",
) -> RankedResult:
    """Rank every feasible legal vector (no objective pruning) and return the best."""
    if objective not in ("fee_first", "client_first"):
        raise ValueError(objective)
    if fee_policy not in ("front_loaded", "proportional"):
        raise ValueError(fee_policy)
    inst = Instance(client, rules, first_payment_date, S, F)
    best_key: tuple | None = None
    best: tuple[int, ...] | None = None
    best_phi: tuple[int, ...] | None = None
    best_cushion: int | None = None
    n = 0
    for vec in inst.vectors():
        bal = inst.replay(vec)
        phi = inst.phi(bal, vec if fee_policy == "proportional" else None)
        if phi is None:
            continue
        n += 1
        cushion = min(inst.windows(bal)) - F
        if objective == "fee_first":
            key: tuple = (phi, -len(vec))
        else:
            key = (cushion, -vec[-1], phi, -len(vec))
        if best_key is None or key > best_key:
            best_key, best, best_phi, best_cushion = key, vec, phi, cushion
    return RankedResult(best_key is not None, best_key, best, best_phi, best_cushion, n)
