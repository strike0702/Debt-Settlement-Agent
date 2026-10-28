"""Schedule objectives (``fee_first`` / ``client_first``) and the client-first exact search.

The engine picks one plan among every legal, feasible payment vector. *Which* one is
the objective, an explicit sort key (maximised), written in docs/ENGINE.md before
this code:

    fee_first    = (Φ, -k, min_slack, total_slack, -cliff, -shape_order, -payments)
    client_first = (cushion, -p_max, Φ, -k, min_slack, total_slack, -cliff, -shape_order,
                    -payments)

``cushion = min_{1<=j<=M} w_j - F`` (the lowest balance with the whole program fee held
back; the post-fee slack is degenerate under the front-loaded fee, see the docs) and
``p_max = p_k``. Φ is placed under ``Problem.fee_policy`` (``front_loaded`` or
``proportional``). ``fee_first`` is the take-home ranking and stays in ``search.Search``
(Phase 57b). This module holds the names, the key builders and ``ClientFirst``. It does
not simulate ledgers or build schedule rows (``engine.py`` does), and the rescue
amounts L* / X* do not depend on the objective (``rescue.py``).

``ClientFirst`` is exact in three stages. Fix k. By the prefix-cap identity cushion
depends on the vector only through its prefix sums, and pointwise smaller prefix sums
never lower it. "cushion >= C and feasible" is the prefix caps
``P_j <= U_j = capw_j - max(F + C, b)`` (j < k) plus a test on the windows after the
last payment, which depend on k only.

1. ``C*``: "some legal vector has cushion >= C" is monotone in C. Each test scans a
   dominating family (every legal vector has a member with pointwise smaller or equal
   prefix sums: ``family(k, INF)`` is the Phase 57 reference family) under the caps
   for C, which prune it hard, so C is bisected.
2. ``T*``: the smallest largest payment under the ``C*`` caps. ``-p_max`` prefers
   *larger* prefix sums (``p_k = S - P_{k-1}``), so the bounds above do not apply to it.
   "Some legal vector with every payment <= T meets the caps" is monotone in T, so T is
   searched upward from a lower bound (``ceil(S/k)``, ``f_k`` and, for each j < k,
   ``ceil((S - U_j)/(k - j))``, since the k - j later payments are each <= T). Each test
   scans ``family(k, T)``, which dominates every legal vector with all payments <= T:

   - Even rules: one vector.
   - Balloon-type vectors (last payment above the one before): ``g_T`` puts
     ``x = min(T, S - f_1 - .. - f_{k-1})`` last and fills positions k-1, k-2, .. up to
     ``x - 1`` over their floors. For any balloon q with ``q_k = x' <= T`` the sum of its
     last t payments is at most ``min(x' + (t-1)(x'-1), S - G_{k-t})``, which grows with
     x', and ``g_T`` reaches that bound at x. So ``g_T`` dominates them all.
   - Staircases (at most s levels), the shift argument with a capped absorbing level:
     for l < m, lowering ``v_l`` by ``D_l = c_m/gcd(c_l, c_m)`` and raising ``v_m`` by
     ``c_l/gcd(c_l, c_m)`` keeps sum, order, floors and run count and lowers every prefix
     sum weakly (``search.py``). Apply it for l = 1..m-1 while ``v_l - D_l >= lo_l`` and
     ``v_m`` stays <= T. Either every level ends in its box ``[lo_l, lo_l + D_l)``. That
     is the box family (last two levels in closed form) with ``v_m <= T``. Or a shift was
     blocked by T, so ``v_m > T - c_l/g >= T - (n - c_m)``. Then ``v_m`` is one of fewer
     than ``n - c_m`` values next to T, and the runs before it form a staircase with sum
     ``S - c_m v_m``, levels <= ``v_m - 1`` and at most s - 1 levels. That is dominated
     recursively by the same family with cap ``v_m - 1``.
3. Among vectors meeting the ``C*`` caps with every payment <= ``T*``: the best Φ, then
   fewer payments, then the fee-first tail over ``family(k, T*)``. Under
   ``front_loaded`` Φ and the slack terms are monotone, so the dominating family gives
   Φ* (and k) exactly. Under ``proportional`` Φ is not monotone (the ``floor(F*P_j/S)``
   cap rises with the prefix sums): exact over the family only.

When a cap equals the floors' prefix sum (``U_j = G_j``, tight cash), p_1..p_j must be
exactly the floors, so their distinct values are levels already spent; a sub-problem
that needs more levels than it has left is cut at once.

A staircase sub-problem (positions 1..n, sum R, cap W, at most smax levels) depends only
on those four numbers and the caps, so one that yields nothing is remembered until the
caps change. It is recorded only when no branch-and-bound hook was active: a hook can
cut vectors, so an empty result under a hook proves nothing.

Pruning, and why it cannot lose the optimum: a branch is cut when a known prefix sum, or
a lower bound on one (later levels above the current one and at or above their floors,
or at least ``R - (n - j) * cap`` when the later payments are capped), already passes
``U_j``. That holds for every extension. Each such bound rises with the level being
scanned in the direction of the scan, so the scan stops there. Checked against
``tests/engine/oracle.py`` (every legal vector) and ``tests/engine/reference.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from itertools import accumulate
from math import gcd

from feasibility.constants import SHAPE_TIEBREAK_ORDER
from feasibility.models import CreditorRules
from feasibility.problem import Problem
from feasibility.scoring import cliff_ratio
from feasibility.search import INF, Key4, Search
from feasibility.shapes import even_vector, is_statically_valid, label_shape

FEE_FIRST = "fee_first"
CLIENT_FIRST = "client_first"
OBJECTIVES: tuple[str, ...] = (FEE_FIRST, CLIENT_FIRST)

FRONT_LOADED = "front_loaded"
PROPORTIONAL = "proportional"
FEE_POLICIES: tuple[str, ...] = (FRONT_LOADED, PROPORTIONAL)


def check_objective(objective: str) -> str:
    """Return ``objective`` if known, else raise ``ValueError``."""
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown objective: {objective!r} (want one of {OBJECTIVES})")
    return objective


def check_fee_policy(policy: str) -> str:
    """Return ``policy`` if known, else raise ``ValueError``."""
    if policy not in FEE_POLICIES:
        raise ValueError(f"unknown fee_policy: {policy!r} (want one of {FEE_POLICIES})")
    return policy


def fee_first_key(k4: Key4, vec: tuple[int, ...], rules: CreditorRules) -> tuple:
    """``scoring.score_key`` from its first four terms and the vector (the tie-break tail)."""
    label = label_shape(vec, rules)
    return (*k4, -cliff_ratio(vec), -SHAPE_TIEBREAK_ORDER[label], tuple(-x for x in vec))


def client_first_key(
    cushion: int, k4: Key4, vec: tuple[int, ...], rules: CreditorRules
) -> tuple:
    """``(cushion, -p_max, *fee_first_key)``; see the module docstring."""
    return (cushion, -vec[-1], *fee_first_key(k4, vec, rules))


def cushion_of(search: Search, P: list[int], k: int) -> int:
    """``min_j w_j - F`` over windows 1..M for prefix sums ``P_1..P_{k-1}`` (any buffer)."""
    capw = search.capw
    low = min(search._tail(k))
    for j in range(1, k):
        wj = capw[j] - P[j - 1]
        if wj < low:
            low = wj
    return low - search.F


def schedule_cushion(problem: Problem, payments: tuple[int, ...] | list[int]) -> int:
    """Cushion (``min_j w_j - F``) of one payment vector on ``problem``'s cadence.

    For reports (the eval's PRIVATE ``min_cushion_cents``); never spoken.
    """
    vec = list(payments)
    search = Search(problem, sum(vec))
    return cushion_of(search, list(accumulate(vec)), len(vec))


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


class ClientFirst:
    """Exact ``client_first`` search for one ``Search`` (problem, S) under ``buffer``."""

    def __init__(self, search: Search, buffer: int) -> None:
        self.s = search
        self.b = buffer
        self.rules = search.rules
        self.S = search.S
        self.F = search.F
        self.f = search.f
        self.G = search.G
        self.capw = search.capw
        self.M = search.M
        self.U: list[int] = [INF] * (self.M + 1)  # live caps on P_j (j < k)
        self.k = 0  # length of the vectors being generated
        self.c_vec: tuple[int, ...] | None = None  # a stage-1 vector reaching C*
        # Optional branch-and-bound hook: gets lower bounds on P_1..P_{k-1}, True cuts.
        # It must only get worse as prefix sums rise (stage 3, front-loaded key4).
        self.hook: Callable[[list[int]], bool] | None = None
        self.fixed: list[int] = []  # P_j already fixed by an outer capped last run
        # Staircase sub-problems (k, n, R, W, smax) with no vector under the current caps.
        self._dead: set[tuple[int, int, int, int, int]] = set()
        self.jf = 0  # see ``family``
        self.forced_levels: list[int] = [0]

    # --- per-k constants ---------------------------------------------------------

    def tail_low(self, k: int) -> int | None:
        """Lowest window from t_k on (None when those windows already fail feasibility)."""
        if self.s._phi_tail(k, self.b) is None:
            return None
        return min(self.s._tail(k))

    def _set_caps(self, C: int | None) -> None:
        """Caps for cushion >= C (feasible only when ``C`` is None)."""
        need = self.b if C is None else max(self.F + C, self.b)
        capw = self.capw
        self.U = [INF] + [capw[j] - need for j in range(1, self.M + 1)]
        self._dead = set()

    # --- the capped dominating family ----------------------------------------------

    def family(self, k: int, T: int) -> Iterator[tuple[int, ...]]:
        """Legal length-k vectors with every payment <= T meeting ``self.U`` that dominate
        every such legal vector (module docstring)."""
        self.k = k
        self.fixed = [0] * (k + 1)
        # Largest j < k whose cap equals the floors' sum: p_1..p_j are exactly the floors.
        self.jf = max((j for j in range(1, k) if self.U[j] <= self.G[j]), default=0)
        self.forced_levels = [0] * (self.jf + 1)
        for j in range(1, self.jf + 1):
            new_level = j == 1 or self.f[j] != self.f[j - 1]
            self.forced_levels[j] = self.forced_levels[j - 1] + new_level
        rules, S, f = self.rules, self.S, self.f
        k_max = self.s.k_max
        if rules.even_pays:
            vec = even_vector(S, k)
            if vec[-1] <= T and self._caps_ok(vec) and is_statically_valid(
                k, vec, S, k_max, f, rules, "even"
            ):
                yield vec
            return
        if rules.is_ballooning_allowed:
            vec = self._balloon(k, T)
            if vec is not None and self._caps_ok(vec):
                yield vec
        for vec in self._stair(k, S, T, min(rules.max_segments, k)):
            if is_statically_valid(k, vec, S, k_max, f, rules, label_shape(vec, rules)):
                yield vec

    def _caps_ok(self, vec: tuple[int, ...]) -> bool:
        U = self.U
        run = 0
        for j in range(1, len(vec)):
            run += vec[j - 1]
            if run > U[j]:
                return False
        return True

    def _balloon(self, k: int, T: int) -> tuple[int, ...] | None:
        """``g_T``: the balloon-type vector with all payments <= T and the largest suffix sums."""
        S, f, G = self.S, self.f, self.G
        if k == 1:
            return (S,) if f[1] <= S <= T else None
        x = min(T, S - G[k - 1])
        if x < f[k] or f[k - 1] > x - 1:
            return None
        extra = S - x - G[k - 1]
        out = list(f[1:k])
        for i in range(k - 2, -1, -1):
            if extra == 0:
                break
            add = min(extra, x - 1 - out[i])
            out[i] += add
            extra -= add
        if extra:
            return None
        return (*out, x)

    def _cap_ok(self, j: int, P: int) -> bool:
        return j >= self.k or P <= self.U[j]

    def _stair(self, n: int, R: int, W: int, smax: int) -> Iterator[tuple[int, ...]]:
        """Staircases on positions 1..n: sum R, levels <= W, at most ``smax`` levels, caps
        met on P_1..P_{n-1} (the caller checks ``P_n = R``)."""
        f, G = self.f, self.G
        if smax < 1 or R < G[n] or R > n * W:
            return
        if smax == 1:  # one level: R/n everywhere
            v = R // n
            if R == v * n and f[n] <= v <= W and all(
                self._cap_ok(j, j * v) for j in range(1, n)
            ):
                yield (v,) * n
            return
        jj = min(self.jf, n)
        if jj and self.forced_levels[jj] + (R - G[jj] != (n - jj) * f[jj]) > smax:
            return  # caps pin P_1..P_jj to the floors: those levels are spent
        key = (self.k, n, R, W, smax)
        if key in self._dead:
            return
        found = False
        for vec in self._stair_cases(n, R, W, smax):
            found = True
            yield vec
        if not found and self.hook is None:  # a hook may have cut: not proof of none
            self._dead.add(key)

    def _stair_cases(self, n: int, R: int, W: int, smax: int) -> Iterator[tuple[int, ...]]:
        """The two cases of ``_stair`` for ``smax >= 2`` (module docstring)."""
        f, G = self.f, self.G
        for j in range(1, n):  # P_j >= floors and >= R - (n - j) * W
            low = G[j] if G[j] > R - (n - j) * W else R - (n - j) * W
            if not self._cap_ok(j, low):
                return
        # Case 1: box family with the last level <= W.
        if R % n == 0:
            v = R // n
            if f[n] <= v <= W and all(self._cap_ok(j, j * v) for j in range(1, n)):
                yield (v,) * n
        for cm in range(1, n):
            yield from self._box(n, R, W, smax, cm, 0, 0, 0, 0, [])
        # Case 2: last run (c, V) with V next to W, the rest capped at V - 1.
        if W < INF:
            for c in range(1, n):
                n2 = n - c
                v_hi = min(W, (R - G[n2]) // c)
                v_lo = max(W - n2 + 1, f[n], _ceil_div(R + n2, n))
                for V in range(v_hi, v_lo - 1, -1):
                    R2 = R - c * V
                    # Smaller V only raises these prefix sums: stop at the first miss.
                    if not all(self._cap_ok(n2 + o, R2 + o * V) for o in range(c)):
                        break
                    if not all(
                        self._cap_ok(j, max(G[j], R2 - (n2 - j) * (V - 1)))
                        for j in range(1, n2)
                    ):
                        break
                    fixed = self.fixed
                    for o in range(c):
                        if n2 + o < self.k:
                            fixed[n2 + o] = R2 + o * V
                    if self.hook is not None:
                        lower = [max(G[j], R2 - (n2 - j) * (V - 1)) for j in range(1, n2)]
                        if self.hook(lower + fixed[n2 : self.k]):
                            break
                    for pre in self._stair(n2, R2, V - 1, smax - 1):
                        yield (*pre, *([V] * c))

    def _close(
        self, n: int, R: int, W: int, pos: int, li: int, v_prev: int, A: int,
        levels: list[tuple[int, int]], ca: int, cb: int,
    ) -> tuple[int, ...] | None:
        """Last two runs in closed form (smallest congruent ``v_{m-1}``), last level <= W."""
        f = self.f
        fa = f[pos + ca]
        lo = fa if li == 0 or fa > v_prev else v_prev + 1
        rest = R - A
        g = gcd(ca, cb)
        if rest % g:
            return None
        mod = cb // g
        if mod == 1:
            v = lo
        else:
            v0 = (rest // g) * pow(ca // g, -1, mod) % mod
            v = lo + (v0 - lo) % mod
        vb = (rest - ca * v) // cb
        if vb < f[n] or vb <= v or vb > W:
            return None
        P = A
        for o in range(1, ca + 1):
            if not self._cap_ok(pos + o, P + o * v):
                return None
        P += ca * v
        for o in range(1, cb):
            if not self._cap_ok(pos + ca + o, P + o * vb):
                return None
        out: list[int] = []
        for c, lv in levels:
            out.extend([lv] * c)
        out.extend([v] * ca)
        out.extend([vb] * cb)
        return tuple(out)

    def _box(
        self, n: int, R: int, W: int, smax: int, cm: int, pos: int, li: int, v_prev: int,
        A: int, levels: list[tuple[int, int]],
    ) -> Iterator[tuple[int, ...]]:
        """DFS over early runs in their boxes; the last run has length ``cm``."""
        f = self.f
        r = n - pos
        if r - cm >= 1 and li + 2 <= smax:
            vec = self._close(n, R, W, pos, li, v_prev, A, levels, r - cm, cm)
            if vec is not None:
                yield vec
        if li + 3 > smax:
            return
        for c in range(1, r - cm):
            end = pos + c
            fe = f[end]
            lo = fe if li == 0 or fe > v_prev else v_prev + 1
            D = cm // gcd(c, cm)
            for v in range(lo, lo + D):
                rest = 0
                nxt = v + 1
                run = A + c * v
                bad = False
                for o in range(1, c + 1):  # this run's prefix sums
                    if not self._cap_ok(pos + o, A + o * v):
                        bad = True
                        break
                chain: list[int] = []
                if not bad:
                    lowP = run
                    for i in range(end + 1, n + 1):  # later payments > v, >= floors
                        add = f[i] if f[i] > nxt else nxt
                        rest += add
                        if i < n:
                            lowP += add
                            chain.append(lowP)
                            if not self._cap_ok(i, lowP):
                                bad = True
                                break
                if bad or run + rest > R:
                    break
                if run + (n - end) * W < R:
                    continue  # v too small for the later (capped) payments
                if self.hook is not None:
                    known: list[int] = []
                    P = 0
                    for cl, lv in levels:
                        for _ in range(cl):
                            P += lv
                            known.append(P)
                    known.extend(A + o * v for o in range(1, c + 1))
                    lower = known + chain + self.fixed[n : self.k]
                    if self.hook(lower[: self.k - 1]):
                        break
                yield from self._box(
                    n, R, W, smax, cm, end, li + 1, v, run, [*levels, (c, v)]
                )

    # --- the three stages ---------------------------------------------------------

    def best_cushion(self) -> int | None:
        """Stage 1: ``C*`` over every legal feasible vector (None: nothing feasible).

        "Some vector has cushion >= C" is monotone in C, and the caps for C prune the
        family hard, so C is bisected between the cushion of a vector already found and
        the floors bound. A hit raises the low end to that vector's own cushion.
        """
        s, F, G, capw = self.s, self.F, self.G, self.capw
        order: list[tuple[int, int]] = []
        for k in range(1, s.k_max + 1):
            low = self.tail_low(k)
            if low is None:
                continue
            bound = min([low] + [capw[j] - G[j] for j in range(1, k)]) - F
            order.append((bound, k))
        order.sort(reverse=True)

        def find(C: int | None) -> tuple[int, ...] | None:
            self._set_caps(C)
            for bound, k in order:
                if C is not None and bound < C:
                    break
                vec = next(iter(self.family(k, INF)), None)
                if vec is not None:
                    return vec
            return None

        vec = find(None)
        if vec is None:
            return None
        lo, hi = cushion_of(s, list(accumulate(vec)), len(vec)), order[0][0]
        self.c_vec = vec
        while lo < hi:
            mid = (lo + hi + 1) // 2
            vec = find(mid)
            if vec is None:
                hi = mid - 1
            else:
                lo, self.c_vec = cushion_of(s, list(accumulate(vec)), len(vec)), vec
        return lo

    def _t_lower(self, k: int) -> int:
        S, U = self.S, self.U
        lb = max(_ceil_div(S, k), self.f[k])
        for j in range(1, k):
            lb = max(lb, _ceil_div(S - U[j], k - j))
        return lb

    def _exists(self, k: int, T: int) -> bool:
        return next(iter(self.family(k, T)), None) is not None

    def min_max_payment(self, k: int, lo: int, hi: int) -> int:
        """Stage 2 for one k: the smallest T in [lo, hi] with a capped vector, given one at hi.

        Gallops up from ``lo`` (the answer is usually near the lower bound), then bisects.
        """
        T, fail, step = lo, lo - 1, 1
        while T < hi and not self._exists(k, T):
            fail = T
            T = min(hi, T + step)
            step *= 2
        lo = fail + 1
        while lo < T:  # smallest success in (fail, T]
            mid = (lo + T) // 2
            if self._exists(k, mid):
                T = mid
            else:
                lo = mid + 1
        return T

    def solve(self) -> tuple[tuple[int, ...], str, int, int] | None:
        """(vector, label, C*, T*) of the ``client_first`` winner; None if nothing is feasible."""
        C = self.best_cushion()
        if C is None:
            return None
        s, F, G = self.s, self.F, self.G
        self._set_caps(C)
        ks: list[tuple[int, int]] = []
        for k in range(1, s.k_max + 1):
            low = self.tail_low(k)
            if low is None or low - F < C:
                continue
            if any(G[j] > self.U[j] for j in range(1, k)):
                continue
            ks.append((self._t_lower(k), k))
        ks.sort()
        # The stage-1 vector meets the C* caps: its largest payment is a valid start.
        assert self.c_vec is not None
        t_best = self.c_vec[-1]
        tied: list[int] = []
        for lb, k in ks:
            if lb > t_best:
                break
            # One test per k decides "can beat the best so far"; only then search T.
            if lb < t_best and self._exists(k, t_best - 1):
                t_best, tied = self.min_max_payment(k, lb, t_best - 1), [k]
            elif self._exists(k, t_best):
                tied.append(k)
        assert tied, "the stage-1 vector's k reaches t_best"
        best_key: tuple | None = None
        best_vec: tuple[int, ...] | None = None
        bound_ok = not s.prop  # key4 of lower prefix sums bounds Φ only when front-loaded
        for k in tied:
            if bound_ok:

                def cut(lower: list[int], k: int = k) -> bool:
                    u = s.key4(lower, k, self.b)
                    return u is None or (best_key is not None and u < best_key[:4])

                self.hook = cut
            for vec in self.family(k, t_best):
                k4 = s.key4(list(accumulate(vec)), k, self.b)
                if k4 is None:
                    continue
                key = fee_first_key(k4, vec, self.rules)
                if best_key is None or key > best_key:
                    best_key, best_vec = key, vec
        self.hook = None
        assert best_vec is not None
        return best_vec, label_shape(best_vec, self.rules), C, t_best
