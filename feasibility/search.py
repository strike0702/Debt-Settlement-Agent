"""Exact payment-vector search: branch and bound over a dominating family (Phase 57b).

``Search(problem, S)`` answers the engine's three questions for one offer total S
without scoring every candidate: the best feasible vector under the fee-first
``scoring.score_key`` (Part 1), and when none is feasible the smallest lump ``L*``
and the smallest uniform draft increment ``X*`` (Part 2). ``engine.evaluate`` calls
it; ``rescue.exact_lump`` / ``rescue.exact_increment`` format the Part 2 answers.
It does not simulate ledgers (``simulate.py``), place fees for the schedule
(``scoring.place_fee``) or build rows (``engine.py``). ``legacy.py`` keeps the old
enumerate-everything path for comparisons.

Why prefix sums are enough. By the prefix-cap identity (``problem.py``) window j of a
length-k vector sees ``base_w[j] - P_min(j,k) - bf*min(j,k)``. So feasibility, Φ
(suffix-min of the window minima, capped at F), the slack terms of ``score_key``, the
lump and the increment depend on a vector only through ``P_1..P_{k-1}``, and pointwise
smaller prefix sums never hurt: Φ weakly up, L and X weakly down, and when Φ is equal
both slack terms weakly up. Hence the key prefix (Φ, -k, min_slack, total_slack) is
monotone (an upper bound on it from lower bounds on the prefix sums is valid), and a
*dominating family* (every legal length-k vector has a member with pointwise smaller
or equal prefix sums) is enough to get Φ*, L* and X* exactly.

Two stages, so outputs only change where the old family was not exact:

1. *Pinned* (the take-home family, same winner). Per composition the take-home
   generator pins levels ``v_1..v_{m-2}`` at their lower bounds and scans the last two.
   Raising ``v_{m-1}`` raises every prefix sum from run m-1 on, so the smallest valid
   ``v_{m-1}`` (closed form, below) strictly beats the rest of its composition on the
   key, and it is also lexicographically smallest (the Part 2 tie-break). The quasi-even
   vector and the floors-pinned balloon are scored as before. So the best key and the
   (L, k, payments) minimum over this stage equal the old engine's.
2. *Box* (staircase rules with ``max_segments >= 3`` only). Phase 57 found the pins on
   ``v_1..v_{m-2}`` lose feasibility, Φ* and rescue minima with tiers. Shift argument
   (from ``tests/engine/reference.py``): for l < m, lowering ``v_l`` by
   ``D_l = c_m / gcd(c_l, c_m)`` and raising ``v_m`` by ``c_l*D_l/c_m`` keeps the sum,
   order, floors and run count and lowers every prefix sum weakly, so vectors with
   ``v_l in [lo_l, lo_l + D_l)`` (``lo_l = max(f_{end of run l}, v_{l-1} + 1)``)
   dominate. For l = m-1 that box holds at most one value with
   ``c_{m-1}*v ≡ S - A (mod c_m)``: the smallest one, then
   ``v_m = (S - A - c_{m-1}v)/c_m >= max(f_k, v + 1)``. A box vector is accepted only
   when it beats stage 1 strictly (Φ for Part 1, L or X for Part 2), so cases the old
   family already solved keep their schedule and rescue rows byte for byte.

Even rules (one vector per k) and balloon rules (the floors-pinned balloon dominates
every legal vector) are exact in stage 1, and so are compositions with at most two
levels. Stage 2 only adds three or more levels.

Pruning, and why it cannot lose the optimum: a partial vector gets lower bounds on its
later prefix sums (later levels are above the current one and at or above their
floors); the bound on the objective from those is valid for every extension and only
gets worse as the current level rises, so the level scan stops at the first failure.
The same holds for the remaining-sum test. Per-k bounds from the floors' prefix sums
skip whole k. ``tests/engine/test_search.py`` checks this against
``tests/engine/reference.py`` (exact, independent) and against ``legacy.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from itertools import accumulate
from math import gcd

from feasibility.constants import SHAPE_TIEBREAK_ORDER
from feasibility.problem import Problem
from feasibility.scoring import cliff_ratio
from feasibility.shapes import even_vector, is_statically_valid, label_shape

INF = 10**18

Key4 = tuple[tuple[int, ...], int, int, int]  # (Φ, -k, min_slack, total_slack)
Prune = Callable[[list[int]], bool]  # lower bounds on P_1..P_{k-1} -> cut this branch


def _prefix(vec: tuple[int, ...]) -> list[int]:
    return list(accumulate(vec))


class Search:
    """Branch and bound for one (problem, S); see the module docstring."""

    def __init__(self, problem: Problem, S: int) -> None:
        rules = problem.rules
        self.problem = problem
        self.rules = rules
        self.S = S
        self.k_max = problem.k_max
        self.M = problem.M
        self.F = problem.program_fee_cents
        self.bf = rules.bank_fee_cents
        self.f = problem.floors
        self.G = list(accumulate(self.f))  # G[i] = f_1 + .. + f_i
        self.pre_min = problem.pre_min
        self.capw = problem.window_caps
        # Lifting early levels only matters for plain staircases with >= 3 levels.
        self.box = (
            not rules.even_pays and not rules.is_ballooning_allowed and rules.max_segments >= 3
        )
        self._tails: dict[int, list[int]] = {}
        self._phi_tails: dict[tuple[int, int], tuple[list[int], int, int, int] | None] = {}

    # --- per-k tail (windows k..M see all of S plus k bank fees) -----------------

    def _tail(self, k: int) -> list[int]:
        tw = self._tails.get(k)
        if tw is None:
            drop = self.S + self.bf * k
            bw = self.problem.base_w
            tw = [bw[j] - drop for j in range(k, self.M + 1)]
            self._tails[k] = tw
        return tw

    def _phi_tail(self, k: int, b: int) -> tuple[list[int], int, int, int] | None:
        """(Φ_k..Φ_M, min slack, total slack, min w) on the tail under buffer b; None if
        infeasible there."""
        key = (k, b)
        if key in self._phi_tails:
            return self._phi_tails[key]
        tw = self._tail(k)
        out: tuple[list[int], int, int, int] | None = None
        if self.pre_min >= b and min(tw) >= b and tw[-1] - b >= self.F:
            F = self.F
            phi = [0] * len(tw)
            run = INF
            ms, ts = INF, 0
            for i in range(len(tw) - 1, -1, -1):
                run = min(run, tw[i])
                phi[i] = min(F, run - b)
                sl = tw[i] - phi[i]
                ms = min(ms, sl)
                ts += sl
            out = (phi, ms, ts, run)
        self._phi_tails[key] = out
        return out

    # --- objectives from prefix sums -------------------------------------------

    def key4(self, P: list[int], k: int, b: int) -> Key4 | None:
        """(Φ, -k, min_slack, total_slack) of ``score_key`` from P_1..P_{k-1} (None: infeasible)."""
        tail = self._phi_tail(k, b)
        if tail is None:
            return None
        phi_t, ms, ts, run = tail  # run: suffix min of w, extended leftwards below
        F, capw = self.F, self.capw
        head = [0] * (k - 1)
        for j in range(k - 1, 0, -1):
            wj = capw[j] - P[j - 1]
            if wj < b:
                return None
            if wj < run:
                run = wj
            ph = F if run - b > F else run - b
            head[j - 1] = ph
            sl = wj - ph
            if sl < ms:
                ms = sl
            ts += sl
        return (tuple(head + phi_t), -k, min(self.pre_min, ms), ts)

    def lump_of(self, P: list[int], k: int) -> int:
        """Smallest lump (credited before every event) that makes the vector feasible."""
        tw = self._tail(k)
        L = max(0, -self.pre_min, -min(tw), self.F - tw[-1])
        capw = self.capw
        for j in range(1, k):
            d = P[j - 1] - capw[j]
            if d > L:
                L = d
        return L

    def _window_x(self, j: int, c: int) -> int:
        """Smallest X keeping every date of window j at or above its requirement."""
        x = 0
        for slack, n in self.problem.window_items[j]:
            need = c - slack
            if need > 0:
                if n == 0:
                    return INF
                q = -((-need) // n)
                if q > x:
                    x = q
        return x

    def _incr_const(self, k: int) -> int:
        x = self._window_x(0, 0)
        drop = self.S + self.bf * k
        for j in range(k, self.M + 1):
            if x >= INF:
                break
            x = max(x, self._window_x(j, drop))
        return x

    def increment_of(self, P: list[int], k: int, const: int) -> int:
        """Smallest uniform draft increment for the vector (INF when none works)."""
        x = const
        bf = self.bf
        for i in range(1, k):
            if x >= INF:
                break
            x = max(x, self._window_x(i, P[i - 1] + bf * i))
        return x

    # --- the families -----------------------------------------------------------

    def _lower(self, P: list[int], v: int, k: int) -> list[int]:
        """Known prefix sums, then lower bounds up to k-1 (later levels > v, >= floors)."""
        out = list(P)
        run = P[-1] if P else 0
        f = self.f
        nxt = v + 1
        for i in range(len(P) + 1, k):
            run += f[i] if f[i] > nxt else nxt
            out.append(run)
        return out

    def _min_rest(self, pos: int, v: int, k: int) -> int:
        f = self.f
        nxt = v + 1
        return sum(f[i] if f[i] > nxt else nxt for i in range(pos + 1, k + 1))

    def _close(
        self, k: int, pos: int, li: int, v_prev: int, A: int, levels: list[tuple[int, int]],
        ca: int, cb: int,
    ) -> tuple[int, ...] | None:
        """Last two runs in closed form: smallest congruent ``v_{m-1}``, then ``v_m``."""
        f = self.f
        fa = f[pos + ca]
        lo = fa if li == 0 or fa > v_prev else v_prev + 1
        R = self.S - A
        g = gcd(ca, cb)
        if R % g:
            return None
        mod = cb // g
        if mod == 1:
            v = lo
        else:
            v0 = (R // g) * pow(ca // g, -1, mod) % mod
            v = lo + (v0 - lo) % mod
        vb = (R - ca * v) // cb
        if vb < f[k] or vb <= v:
            return None
        out: list[int] = []
        for c, lv in levels:
            out.extend([lv] * c)
        out.extend([v] * ca)
        out.extend([vb] * cb)
        return tuple(out)

    def _runs(
        self, k: int, pos: int, li: int, v_prev: int, A: int, P: list[int],
        levels: list[tuple[int, int]], cm: int | None, s: int, prune: Prune,
    ) -> Iterator[tuple[int, ...]]:
        """DFS over run boundaries. ``cm=None``: pinned stage (all m <= s); else box stage
        with the last run of length ``cm`` and m >= 3."""
        f, S = self.f, self.S
        r = k - pos
        if cm is None:
            if li + 1 <= s:
                lo = f[k] if li == 0 else max(f[k], v_prev + 1)
                if (S - A) % r == 0 and (S - A) // r >= lo:
                    out: list[int] = []
                    for c, lv in levels:
                        out.extend([lv] * c)
                    yield tuple(out + [(S - A) // r] * r)
            if li + 2 <= s:
                for ca in range(1, r):
                    vec = self._close(k, pos, li, v_prev, A, levels, ca, r - ca)
                    if vec is not None:
                        yield vec
            last_free = r - 2
        else:
            if li >= 1 and li + 2 <= s and r - cm >= 1:
                vec = self._close(k, pos, li, v_prev, A, levels, r - cm, cm)
                if vec is not None:
                    yield vec
            last_free = r - cm - 1
        if li + 3 > s:
            return
        base_p = P[-1] if P else 0
        for c in range(1, last_free + 1):
            end = pos + c
            fe = f[end]
            lo = fe if li == 0 or fe > v_prev else v_prev + 1
            D = 1 if cm is None else cm // gcd(c, cm)
            for v in range(lo, lo + D):
                if A + c * v + self._min_rest(end, v, k) > S:
                    break
                newP = P + [base_p + v * o for o in range(1, c + 1)]
                if prune(self._lower(newP, v, k)):
                    break
                yield from self._runs(
                    k, end, li + 1, v, A + c * v, newP, [*levels, (c, v)], cm, s, prune
                )

    def _extras(self, k: int) -> Iterator[tuple[int, ...]]:
        """The quasi-even vector and the floors-pinned balloon, filtered like the old generator."""
        S, f, rules = self.S, self.f, self.rules
        vecs = [even_vector(S, k)]
        if rules.is_ballooning_allowed:
            if k == 1:
                vecs.append((S,))
            else:
                residual = S - self.G[k - 1]
                if residual >= max(f[k], f[k - 1]):
                    vecs.append(tuple(f[1:k]) + (residual,))
        for vec in vecs:
            if is_statically_valid(k, vec, S, self.k_max, f, rules, label_shape(vec, rules)):
                yield vec

    def pinned(self, k: int, prune: Prune) -> Iterator[tuple[int, ...]]:
        """Stage 1: every vector of the old family that can decide any objective."""
        if self.rules.even_pays:
            vec = even_vector(self.S, k)
            if vec[0] >= self.f[1] and is_statically_valid(
                k, vec, self.S, self.k_max, self.f, self.rules, "even"
            ):
                yield vec
            return
        s = min(self.rules.max_segments, k)
        yield from self._runs(k, 0, 0, 0, 0, [], [], None, s, prune)
        yield from self._extras(k)

    def boxed(self, k: int, prune: Prune) -> Iterator[tuple[int, ...]]:
        """Stage 2: box vectors with >= 3 levels (empty unless ``self.box``)."""
        if not self.box:
            return
        s = min(self.rules.max_segments, k)
        if s < 3:
            return
        for cm in range(1, k - 1):
            yield from self._runs(k, 0, 0, 0, 0, [], [], cm, s, prune)

    # --- Part 1 -------------------------------------------------------------------

    def _full_key(self, k4: Key4, vec: tuple[int, ...]) -> tuple:
        label = label_shape(vec, self.rules)
        return (
            *k4, -cliff_ratio(vec), -SHAPE_TIEBREAK_ORDER[label], tuple(-x for x in vec)
        )

    def best(self, buffer: int) -> tuple[tuple[int, ...], str] | None:
        """The ``score_key`` winner and its label under ``buffer``; None if nothing is feasible."""
        ks: list[tuple[Key4, int]] = []
        for k in range(1, self.k_max + 1):
            b4 = self.key4(self.G[1:k], k, buffer)
            if b4 is not None:
                ks.append((b4, k))
        ks.sort(reverse=True)

        best: list = [None, None]  # [full key, vec]

        def consider(vec: tuple[int, ...], k: int, floor_phi: tuple[int, ...] | None) -> None:
            k4 = self.key4(_prefix(vec), k, buffer)
            if k4 is None or (floor_phi is not None and k4[0] <= floor_phi):
                return
            if best[0] is not None and k4 < best[0][:4]:
                return
            full = self._full_key(k4, vec)
            if best[0] is None or full > best[0]:
                best[0], best[1] = full, vec

        def run(stage: Callable, floor_phi: tuple[int, ...] | None) -> None:
            for b4, k in ks:
                if floor_phi is not None and b4[0] <= floor_phi:
                    continue
                if best[0] is not None and b4 < best[0][:4]:
                    continue

                def prune(lower: list[int], k: int = k) -> bool:
                    u = self.key4(lower, k, buffer)
                    return (
                        u is None
                        or (floor_phi is not None and u[0] <= floor_phi)
                        or (best[0] is not None and u < best[0][:4])
                    )

                for vec in stage(k, prune):
                    consider(vec, k, floor_phi)

        run(self.pinned, None)
        if self.box:
            legacy = list(best)
            floor_phi = legacy[0][0] if legacy[0] is not None else None
            best[0] = best[1] = None
            run(self.boxed, floor_phi)
            if best[0] is None:
                best[:] = legacy
        if best[1] is None:
            return None
        return best[1], label_shape(best[1], self.rules)

    # --- Part 2 -------------------------------------------------------------------

    def min_lump(self) -> tuple[int, tuple[int, ...]] | None:
        """(L*, the (L, k, payments)-smallest vector achieving it); None if no legal vector."""
        order = sorted(
            (self.lump_of(self.G[1:k], k), k) for k in range(1, self.k_max + 1)
        )
        best: list = [None]  # (L, k, vec)

        def run(stage: Callable, ceiling: int | None) -> None:
            for bound, k in order:
                if ceiling is not None and bound >= ceiling:
                    continue
                if best[0] is not None and (bound, k) > best[0][:2]:
                    continue

                def prune(lower: list[int], k: int = k) -> bool:
                    L = self.lump_of(lower, k)
                    return (ceiling is not None and L >= ceiling) or (
                        best[0] is not None and (L, k) > best[0][:2]
                    )

                for vec in stage(k, prune):
                    L = self.lump_of(_prefix(vec), k)
                    if ceiling is not None and L >= ceiling:
                        continue
                    cand = (L, k, vec)
                    if best[0] is None or cand < best[0]:
                        best[0] = cand

        run(self.pinned, None)
        if self.box:
            legacy = best[0]
            best[0] = None
            run(self.boxed, legacy[0] if legacy is not None else None)
            if best[0] is None:
                best[0] = legacy
        if best[0] is None:
            return None
        return best[0][0], best[0][2]

    def min_increment(self) -> tuple[int | None, bool]:
        """(X*, or None when every legal vector is unfixable; whether any legal vector exists)."""
        order = []
        for k in range(1, self.k_max + 1):
            const = self._incr_const(k)
            order.append((self.increment_of(self.G[1:k], k, const), k, const))
        order.sort()
        best: list = [INF, False]  # [X, any legal vector seen]

        def run(stage: Callable) -> None:
            for bound, k, const in order:
                if bound >= best[0] and best[1]:
                    continue

                def prune(lower: list[int], k: int = k, const: int = const) -> bool:
                    return best[1] and self.increment_of(lower, k, const) >= best[0]

                for vec in stage(k, prune):
                    best[1] = True
                    X = self.increment_of(_prefix(vec), k, const)
                    if X < best[0]:
                        best[0] = X

        run(self.pinned)
        run(self.boxed)
        return (best[0] if best[0] < INF else None), best[1]
