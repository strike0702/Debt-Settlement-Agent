"""In-process latency sample buffer for ``GET /metrics/summary``.

Filled by the WebSocket handler from per-turn ``latency`` events and client
``timing`` (vad_end_to_first_audio_ms). Not persisted — process-lifetime only.
"""

from __future__ import annotations

import math
from collections import defaultdict
from threading import Lock
from typing import Any

# ``queue_ms`` is LLM limiter wait already inside stt/nlu/nlg (not additive);
# ``engine_ms`` is affordability / term-alt search, outside ``policy_ms``.
_STAGES = (
    "stt_ms",
    "nlu_ms",
    "engine_ms",
    "policy_ms",
    "nlg_ms",
    "queue_ms",
    "server_total_ms",
    "vad_end_to_first_audio_ms",
)


class LatencyBuffer:
    """Thread-safe ring of stage samples; ``summary()`` returns p50/p95/n."""

    def __init__(self, *, maxlen: int = 500) -> None:
        self._maxlen = maxlen
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def record(self, stages: dict[str, float]) -> None:
        with self._lock:
            for key, value in stages.items():
                if key not in _STAGES:
                    continue
                if not isinstance(value, (int, float)):
                    continue
                bucket = self._samples[key]
                bucket.append(float(value))
                if len(bucket) > self._maxlen:
                    del bucket[: len(bucket) - self._maxlen]

    def summary(self) -> dict[str, dict[str, float | int | None]]:
        with self._lock:
            out: dict[str, dict[str, float | int | None]] = {}
            for stage in _STAGES:
                vals = list(self._samples.get(stage, []))
                out[stage] = {
                    "p50": _percentile(vals, 50) if vals else None,
                    "p95": _percentile(vals, 95) if vals else None,
                    "n": len(vals),
                }
            return out


def _percentile(sorted_or_not: list[float], pct: float) -> float:
    if not sorted_or_not:
        return 0.0
    xs = sorted(sorted_or_not)
    if len(xs) == 1:
        return xs[0]
    # Nearest-rank style used by eval metrics.
    k = (pct / 100.0) * (len(xs) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return xs[lo]
    frac = k - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


# Process singleton used by FastAPI lifespan + WS.
LATENCY_BUFFER = LatencyBuffer()


def metrics_summary() -> dict[str, Any]:
    return LATENCY_BUFFER.summary()
