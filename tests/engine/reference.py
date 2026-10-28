"""Exact reference solver over the full legal set at full size (Phase 57 test tool).

``solve(problem, S)`` answers the same questions as ``tests/engine/oracle.py`` (is any
schedule legal under the take-home's binding rules 1–9 feasible; the lexicographically
best cumulative-fee vector Φ*; when infeasible, the smallest lump ``L`` and smallest
uniform draft increment ``X``) but at full size (k up to 24, S in the hundreds of
thousands of cents, up to 4 levels), where the oracle's DFS over every cent cannot run.
It is *not* the engine's generator and shares nothing with ``feasibility/shapes.py`` or
``feasibility/legacy.py``: no level is pinned to its floor. It uses the ``Problem``'s
base run through the prefix-cap identity (``feasibility/problem.py``), and the gap
report replays each witness independently (``oracle.Instance``) and validates it.

Exactness argument. Fix k. By the prefix-cap identity every window minimum is
``base_w[j] - P_min(j,k) - bf*min(j,k)``, so feasibility, Φ (suffix-min of the window
minima, capped at F), the lump ``L`` and the increment ``X`` depend on a vector only
through its prefix sums ``P_1..P_{k-1}`` (``P_j = S`` for j >= k), and each is monotone:
pointwise smaller prefix sums never hurt (Φ weakly up, L and X weakly down). So it is
enough to search a *dominating family*: a set of legal vectors such that every legal
length-k vector q has a family member p with ``P_i <= Q_i`` for all i. Legality (rules
3, 4, 7–9) for a non-decreasing vector is: sum S, ``p_i >= f_i`` where ``f_i`` is the
tier/base floor plus one cent for positions after the token limit (payments equal to the
base minimum form a prefix, so "at most T of them" is "positions > T pay more"), and the
structure rule. Floors are non-decreasing in i.

- Even: one vector per k. Family = that vector when legal.
- Balloon allowed: the floors-pinned vector ``(f_1..f_{k-1}, S - Σf)`` has the smallest
  possible prefix sums of any vector at or above the floors, so it dominates every legal
  vector. If its last payment is not above ``f_{k-1}``, no balloon vector exists and the
  only vector at or above the floors with sum S is the floors vector itself, which is
  legal iff it meets ``max_segments``.
- Staircase (at most s levels): each vector is one composition ``c_1..c_m`` (maximal
  runs, m <= s) with strictly increasing levels ``v_1 < .. < v_m``. Shift argument: for
  l < m, lowering ``v_l`` by ``D_l = c_m / gcd(c_l, c_m)`` and raising ``v_m`` by
  ``c_l*D_l/c_m`` (an integer) keeps the sum, the order, the floors and the run count,
  and lowers every prefix sum weakly (inside run l and before run m by ``o*D_l`` or
  ``c_l*D_l``; inside run m at offset o by ``c_l*D_l*(1 - o/c_m) >= 0``). Applying it for
  l = 1..m-1 in turn, as long as ``v_l - D_l`` stays at or above
  ``lo_l = max(f_{end of run l}, v_{l-1} + 1)``, ends with every
  ``v_l in [lo_l, lo_l + D_l)``. So the box vectors dominate. For l = m-1 the box holds
  at most one value with ``c_{m-1}*v ≡ S - A (mod c_m)``, so the last two levels are
  solved in closed form (smallest such v, then ``v_m = (S - A - c_{m-1}v)/c_m``, which
  must be >= ``max(f_k, v + 1)``; a larger v only lowers ``v_m``).

Pruned, and why it cannot lose the optimum: (1) a remaining-sum bound (the later runs
need at least their strictly increasing floor chain), monotone in the level being
scanned; (2) a partial vector whose known or lower-bounded prefix sums (later levels
are > the current one and >= their floors) already exceed a cap is infeasible for every
extension; (3) branch and bound: a pointwise upper bound on Φ (resp. lower bound on L
or X) for every extension that is no better than the incumbent. Each test only gets
worse as the current level rises, so the scan stops there. Per-k bounds from the
floors-only prefix sums skip whole k the same way. Ties go to the vector found first;
only the value (Φ*, L*, X*) is claimed exact.

Used by ``tests/engine/test_reference.py`` (agreement with the oracle on small cases)
and ``scripts/engine_gap_report.py --mode full``; never by ``app/``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import accumulate, combinations
from math import gcd

from feasibility.models import CreditorRules
from feasibility.problem import Problem

INF = 10**18

Prune = Callable[[list[int], int], bool]


@dataclass(frozen=True)
class RefResult:
    """Exact answer over the full legal set for one (problem, S)."""

    feasible: bool
    phi: tuple[int, ...] | None  # Φ*: cumulative fee by t_1..t_M
    best: tuple[int, ...] | None  # a vector achieving Φ*
    lump_cents: int | None  # minimal L when infeasible; None if feasible or unfixable
    increment_cents: int | None  # minimal X when infeasible; None if feasible or unfixable
    lump_vec: tuple[int, ...] | None = None  # a vector achieving L*
    increment_vec: tuple[int, ...] | None = None  # a vector achieving X*
    scored: int = 0  # family vectors scored (all modes)


def floors_with_tokens(rules: CreditorRules, k: int) -> list[int]:
    """1-indexed position floors: base minimum, tiers, +1 cent after the token limit."""
    out = [0] * (k + 1)
    for i in range(1, k + 1):
        f = rules.min_payment_cents
        for frm, m in rules.min_payment_tiers:
            if i >= frm and m > f:
                f = m
        if i > rules.max_token_pays:
            f = max(f, rules.min_payment_cents + 1)
        out[i] = f
    return out


def _compositions(k: int, m: int) -> Iterator[tuple[int, ...]]:
    """Positive compositions of k into exactly m parts."""
    for cuts in combinations(range(1, k), m - 1):
        bounds = (0, *cuts, k)
        yield tuple(bounds[i + 1] - bounds[i] for i in range(m))


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


class Reference:
    """Dominating-family enumeration plus the three objectives for one (problem, S)."""

    def __init__(self, problem: Problem, S: int, *, prune: bool = True) -> None:
        self.use_prune = prune
        self.problem = problem
        self.S = S
        self.F = problem.program_fee_cents
        self.rules = problem.rules
        self.bf = problem.rules.bank_fee_cents
        self.M = problem.M
        self.k_cap = problem.k_max
        self.f = floors_with_tokens(self.rules, max(self.k_cap, 1))
        self.G = list(accumulate(self.f))  # G[i] = f_1 + .. + f_i
        M = self.M
        base_w = problem.base_w
        self.base_w = base_w
        self.pre_min = problem.pre_min
        # P_j <= capP[j] keeps window j (< k) non-negative.
        self.capP = [0] + [base_w[j] - self.bf * j for j in range(1, M + 1)]
        sfx = [INF] * (M + 2)
        for j in range(M, 0, -1):
            sfx[j] = min(base_w[j], sfx[j + 1])
        self.base_sfx = sfx
        # Per-window (balance - required, drafts landed) for the increment; window 0 is
        # before t_1. ``required`` is F on and after t_M (rule 6), else 0.
        client = problem.client
        drafts = sorted(
            e.date for e in client.ledger if e.type == "credit" and e.date > client.as_of_date
        )
        self.num_drafts = len(drafts)
        cad = problem.cadence
        items: list[list[tuple[int, int]]] = [[] for _ in range(M + 1)]
        j = n = 0
        for d in problem.base.ordered_dates:
            while j < M and cad[j] <= d:
                j += 1
            while n < len(drafts) and drafts[n] <= d:
                n += 1
            req = self.F if j == M and M > 0 else 0
            items[j].append((problem.base.balances[d] - req, n))
        self.win_items = items
        self.scored = 0

    @classmethod
    def structural(cls, rules: CreditorRules, S: int, k_cap: int) -> Reference:
        """Family-only instance (no ledger): ``family(k)`` works, the objectives do not."""
        ref = cls.__new__(cls)
        ref.rules, ref.S, ref.k_cap, ref.use_prune = rules, S, k_cap, False
        ref.f = floors_with_tokens(rules, max(k_cap, 1))
        ref.G = list(accumulate(ref.f))
        ref.scored = 0
        return ref

    # --- the dominating family -------------------------------------------------

    def family(self, k: int, prune: Prune | None = None) -> Iterator[tuple[int, ...]]:
        """Legal length-k vectors that dominate every legal length-k vector (docstring)."""
        rules, f, S = self.rules, self.f, self.S
        if not self.use_prune:
            prune = None
        if rules.even_pays:
            q, r = divmod(S, k)
            vec = tuple([q] * (k - r) + [q + 1] * r)
            if all(vec[i - 1] >= f[i] for i in range(1, k + 1)):
                yield vec
            return
        if rules.is_ballooning_allowed:
            last = S - self.G[k - 1]
            if last < f[k]:
                return
            vec = tuple(f[1:k]) + (last,)
            if k == 1 or last > f[k - 1] or len(set(vec)) <= rules.max_segments:
                yield vec
            return
        for m in range(1, min(rules.max_segments, k) + 1):
            for counts in _compositions(k, m):
                ends = list(accumulate(counts))
                yield from self._levels(counts, ends, 0, 0, 0, [], [], prune)

    def _levels(
        self,
        counts: tuple[int, ...],
        ends: list[int],
        li: int,
        v_prev: int,
        A: int,
        P: list[int],
        levels: list[int],
        prune: Prune | None,
    ) -> Iterator[tuple[int, ...]]:
        f, S = self.f, self.S
        m = len(counts)
        c = counts[li]
        lo = f[ends[li]] if li == 0 else max(f[ends[li]], v_prev + 1)
        if li == m - 1:  # m == 1: the single level is fixed by the sum
            if (S - A) % c == 0 and (S - A) // c >= lo:
                yield self._expand(counts, [*levels, (S - A) // c])
            return
        if li == m - 2:  # last two levels in closed form
            cb = counts[m - 1]
            R = S - A
            g = gcd(c, cb)
            if R % g:
                return
            mod = cb // g
            if mod == 1:
                v = lo
            else:
                v0 = (R // g) * pow(c // g, -1, mod) % mod
                v = lo + (v0 - lo) % mod
            vb = (R - c * v) // cb
            if vb < max(f[ends[-1]], v + 1):
                return
            yield self._expand(counts, [*levels, v, vb])
            return
        D = counts[-1] // gcd(c, counts[-1])
        base_p = P[-1] if P else 0
        for v in range(lo, lo + D):
            need, prev = 0, v
            for l2 in range(li + 1, m):
                prev = max(f[ends[l2]], prev + 1)
                need += counts[l2] * prev
            if A + c * v + need > S:
                break
            newP = P + [base_p + v * o for o in range(1, c + 1)]
            if prune is not None and prune(newP, v):
                break
            yield from self._levels(counts, ends, li + 1, v, A + c * v, newP, [*levels, v], prune)

    @staticmethod
    def _expand(counts: tuple[int, ...], levels: list[int]) -> tuple[int, ...]:
        out: list[int] = []
        for c, v in zip(counts, levels):
            out.extend([v] * c)
        return tuple(out)

    def _lower_prefix(self, P: list[int], v: int, k: int) -> list[int]:
        """Known prefix sums, then lower bounds up to k-1 (later levels > v, >= floors)."""
        out = list(P[: k - 1])
        run = P[-1] if P else 0
        f = self.f
        for i in range(len(P) + 1, k):
            run += max(f[i], v + 1)
            out.append(run)
        return out

    # --- per-k constants ---------------------------------------------------------

    def _tail_w(self, k: int) -> list[int]:
        """Window minima for windows k..M (they see all of S plus k bank fees)."""
        drop = self.S + self.bf * k
        return [self.base_w[j] - drop for j in range(k, self.M + 1)]

    def _tail_ok(self, k: int) -> bool:
        tw = self._tail_w(k)
        return self.pre_min >= 0 and min(tw) >= 0 and tw[-1] >= self.F

    def _phi_from_prefix(self, prefix: list[int], k: int) -> tuple[int, ...] | None:
        """Φ for prefix sums P_1..P_{k-1} (or a pointwise bound on them); None if a window < 0."""
        w = [self.capP[i] - prefix[i - 1] for i in range(1, k)] + self._tail_w(k)
        if self.pre_min < 0 or min(w) < 0 or w[-1] < self.F:
            return None
        out = [0] * self.M
        run = self.F
        for j in range(self.M - 1, -1, -1):
            run = min(run, w[j])
            out[j] = run
        return tuple(out)

    @staticmethod
    def _prefix(vec: tuple[int, ...]) -> list[int]:
        return list(accumulate(vec))

    # --- objectives ---------------------------------------------------------------

    def phi_of(self, vec: tuple[int, ...]) -> tuple[int, ...] | None:
        """Φ of one family vector (None when it is infeasible)."""
        return self._phi_from_prefix(self._prefix(vec), len(vec))

    def _lump_const(self, k: int) -> int:
        tw = self._tail_w(k)
        return max(0, -self.pre_min, -min(tw), self.F - tw[-1])

    def _lump_from_prefix(self, prefix: list[int], k: int) -> int:
        worst = max((prefix[i - 1] - self.capP[i] for i in range(1, k)), default=0)
        return max(self._lump_const(k), worst)

    def lump_of(self, vec: tuple[int, ...]) -> int:
        """Smallest lump (credited before every event) that makes ``vec`` feasible."""
        return self._lump_from_prefix(self._prefix(vec), len(vec))

    def _window_x(self, j: int, c: int) -> int:
        """Smallest X keeping every date of window j at or above its requirement."""
        x = 0
        for slack, n in self.win_items[j]:
            need = c - slack
            if need > 0:
                if n == 0:
                    return INF
                x = max(x, _ceil_div(need, n))
        return x

    def _incr_const(self, k: int) -> int:
        x = self._window_x(0, 0)
        drop = self.S + self.bf * k
        for j in range(k, self.M + 1):
            x = max(x, self._window_x(j, drop))
        return x

    def _incr_from_prefix(self, prefix: list[int], k: int, const: int) -> int:
        x = const
        for i in range(1, min(k, len(prefix) + 1)):
            x = max(x, self._window_x(i, prefix[i - 1] + self.bf * i))
            if x >= INF:
                break
        return x

    def increment_of(self, vec: tuple[int, ...]) -> int:
        """Smallest uniform draft increment that makes ``vec`` feasible (INF if none)."""
        k = len(vec)
        return self._incr_from_prefix(self._prefix(vec), k, self._incr_const(k))

    # --- searches ---------------------------------------------------------------

    def best_phi(self) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
        """Φ* and a vector achieving it, or None when no legal vector is feasible."""
        ks: list[tuple[tuple[int, ...], int]] = []
        for k in range(1, self.k_cap + 1):
            if not self._tail_ok(k):
                continue
            bound = self._phi_from_prefix(self.G[1:k], k)
            if bound is not None:
                ks.append((bound, k))
        ks.sort(reverse=True)
        best: tuple[int, ...] | None = None
        best_vec: tuple[int, ...] | None = None
        for bound, k in ks:
            if self.use_prune and best is not None and bound <= best:
                continue

            def prune(P: list[int], v: int, k: int = k) -> bool:
                u = self._phi_from_prefix(self._lower_prefix(P, v, k), k)
                return u is None or (best is not None and u <= best)

            for vec in self.family(k, prune):
                self.scored += 1
                phi = self.phi_of(vec)
                if phi is not None and (best is None or phi > best):
                    best, best_vec = phi, vec
        if best is None:
            return None
        assert best_vec is not None
        return best, best_vec

    def min_lump(self) -> tuple[int, tuple[int, ...]] | None:
        """L* over every legal vector and one vector achieving it; None if none is legal."""
        order = sorted(
            (max(self._lump_const(k), self._lump_from_prefix(self.G[1:k], k)), k)
            for k in range(1, self.k_cap + 1)
        )
        best: int | None = None
        best_vec: tuple[int, ...] | None = None
        for bound, k in order:
            if self.use_prune and best is not None and bound >= best:
                continue

            def prune(P: list[int], v: int, k: int = k) -> bool:
                return best is not None and (
                    self._lump_from_prefix(self._lower_prefix(P, v, k), k) >= best
                )

            for vec in self.family(k, prune):
                self.scored += 1
                L = self.lump_of(vec)
                if best is None or L < best:
                    best, best_vec = L, vec
        if best is None:
            return None
        assert best_vec is not None
        return best, best_vec

    def min_increment(self) -> tuple[int, tuple[int, ...]] | None:
        """X* over every legal vector and one vector achieving it; None if unfixable."""
        order = []
        for k in range(1, self.k_cap + 1):
            const = self._incr_const(k)
            order.append((self._incr_from_prefix(self.G[1:k], k, const), k, const))
        order.sort()
        best: int | None = None
        best_vec: tuple[int, ...] | None = None
        for bound, k, const in order:
            if bound >= INF or (self.use_prune and best is not None and bound >= best):
                continue

            def prune(P: list[int], v: int, k: int = k, const: int = const) -> bool:
                x = self._incr_from_prefix(self._lower_prefix(P, v, k), k, const)
                return x >= INF or (best is not None and x >= best)

            for vec in self.family(k, prune):
                self.scored += 1
                X = self._incr_from_prefix(self._prefix(vec), k, const)
                if X < INF and (best is None or X < best):
                    best, best_vec = X, vec
        if best is None:
            return None
        assert best_vec is not None
        return best, best_vec


def solve(problem: Problem, S: int, *, prune: bool = True) -> RefResult:
    """Exact Part 1 (Φ*) and, when infeasible, exact Part 2 minima for offer total S.

    ``prune=False`` scores the whole dominating family (self-check of the pruning).
    """
    ref = Reference(problem, S, prune=prune)
    found = ref.best_phi()
    if found is not None:
        phi, vec = found
        return RefResult(True, phi, vec, None, None, scored=ref.scored)
    lump = ref.min_lump()
    incr = ref.min_increment()
    return RefResult(
        feasible=False,
        phi=None,
        best=None,
        lump_cents=lump[0] if lump else None,
        increment_cents=incr[0] if incr else None,
        lump_vec=lump[1] if lump else None,
        increment_vec=incr[1] if incr else None,
        scored=ref.scored,
    )
