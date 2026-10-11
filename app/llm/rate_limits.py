"""Per-key rate limiters used by ``app.llm.client.LLMClient``.

``_TokenBucket`` enforces a provider's requests-per-minute (and optional
minimum gap between calls) with a small burst, so one turn's back-to-back
calls (STT, NLU, NLG) do not queue. ``_TpmBudget`` enforces tokens per minute
for one key and model: it charges an estimate before the call and reconciles
to the actual usage after. Both are async, monotonic-clock based, and know
nothing about providers or routes.
"""

from __future__ import annotations

import asyncio
import time

# Burst capacity cap: a turn makes at most ~3 calls (STT, NLU, NLG) back to back.
_BURST_CAP = 5.0


class _TokenBucket:
    """Async token bucket; ``rpm == 0`` disables limiting.

    Starts full with burst capacity ``min(rpm, 5)`` and refills ``rpm/60`` tokens
    per second, so a turn's back-to-back calls do not wait while the sustained
    rate still respects ``rpm``.
    """

    def __init__(self, rpm: float, min_interval_s: float = 0.0) -> None:
        self.rpm = rpm
        self.min_interval_s = min_interval_s
        self._capacity = max(1.0, min(rpm, _BURST_CAP)) if rpm > 0 else float("inf")
        self._tokens = self._capacity
        self._refill_per_s = rpm / 60.0 if rpm > 0 else 0.0
        self._last = time.monotonic()
        self._last_acquire = 0.0
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        if self.rpm <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_s)

    def eta(self) -> float:
        """Seconds until ``acquire`` would return without waiting (0 when ready)."""
        self._refill()
        wait = 0.0
        if self.min_interval_s > 0 and self._last_acquire > 0:
            wait = max(0.0, self.min_interval_s - (time.monotonic() - self._last_acquire))
        if self.rpm > 0 and self._tokens < 1.0 and self._refill_per_s > 0:
            wait = max(wait, (1.0 - self._tokens) / self._refill_per_s)
        return wait

    async def acquire(self) -> None:
        """Wait for a token (and ``min_interval_s``); cancellation consumes none."""
        async with self._lock:
            while True:
                self._refill()
                wait_interval = 0.0
                if self.min_interval_s > 0 and self._last_acquire > 0:
                    since = time.monotonic() - self._last_acquire
                    if since < self.min_interval_s:
                        wait_interval = self.min_interval_s - since
                if self.rpm <= 0:
                    if wait_interval > 0:
                        await asyncio.sleep(wait_interval)
                    self._last_acquire = time.monotonic()
                    return
                if self._tokens >= 1.0 and wait_interval <= 0:
                    self._tokens -= 1.0
                    self._last_acquire = time.monotonic()
                    return
                wait_token = 0.0
                if self._tokens < 1.0 and self._refill_per_s > 0:
                    wait_token = (1.0 - self._tokens) / self._refill_per_s
                await asyncio.sleep(max(wait_interval, wait_token, 0.001))


class _TpmBudget:
    """Tokens-per-minute budget for one key and model (capacity ``tpm``, refill ``tpm/60``/s).

    ``acquire(cost)`` waits until ``cost`` tokens (capped at capacity) are free and
    charges them; ``reconcile`` charges the difference to actual ``usage``, so the
    balance may go negative after an under-estimate and the next caller waits.
    """

    def __init__(self, tpm: float) -> None:
        self._capacity = float(tpm)
        self._tokens = self._capacity
        self._refill_per_s = tpm / 60.0
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._refill_per_s)
        self._last = now

    def eta(self, cost: int) -> float:
        """Seconds until ``cost`` tokens are available (0 when ready)."""
        self._refill()
        need = min(float(cost), self._capacity)
        return max(0.0, (need - self._tokens) / self._refill_per_s)

    async def acquire(self, cost: int) -> None:
        while (wait := self.eta(cost)) > 0:
            await asyncio.sleep(max(wait, 0.001))
        self._tokens -= cost

    def reconcile(self, charged: int, actual: int) -> None:
        self._refill()
        self._tokens = min(self._capacity, self._tokens - (actual - charged))
