"""Fit the reveal table from ``pace.json``: least squares on token features, no paid calls.

Prints the table and its leave-one-out error (estimated minus real clip length).

    uv run python docs/eval/voice_reveal_20261109/fit_pace.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

ROWS = json.loads(Path(__file__).with_name("pace.json").read_text())
NAMES = ("letter", "money", "money_cents", "percent", "number", "comma", "sentence")


def features(text: str) -> list[float]:
    """Counts: letters, money tokens, ... (order of ``NAMES``)."""
    f = dict.fromkeys(NAMES, 0.0)
    f["sentence"] = 1
    for tok in text.split():
        bare = tok.rstrip(".,?!:;")
        if tok.endswith((",", ":", ";")):
            f["comma"] += 1
        if bare.startswith("$"):
            digits = sum(c.isdigit() for c in bare.split(".")[0])
            f["money"] += digits
            f["money_cents"] += 1 if "." in bare else 0
        elif bare.endswith("%"):
            f["percent"] += 1
        elif re.search(r"\d", bare):
            f["number"] += sum(c.isdigit() for c in bare)
        else:
            f["letter"] += sum(c.isalpha() for c in bare)
    return [f[n] for n in NAMES]


def fit(rows: list[dict]) -> np.ndarray:
    x = np.array([features(r["text"]) for r in rows])
    y = np.array([r["ms"] - r["lead_ms"] for r in rows], dtype=float)
    return np.linalg.lstsq(x, y, rcond=None)[0]


def main() -> None:
    table = fit(ROWS)
    print({n: round(float(v), 1) for n, v in zip(NAMES, table, strict=True)})
    errs = []
    for i, r in enumerate(ROWS):
        t = fit(ROWS[:i] + ROWS[i + 1 :])
        est = float(np.dot(features(r["text"]), t))
        errs.append(est - (r["ms"] - r["lead_ms"]))
    e = np.array(errs)
    print(
        f"sentences {len(ROWS)}; leave-one-out error ms: median {np.median(e):.0f}, "
        f"median abs {np.median(abs(e)):.0f}, p90 abs {np.percentile(abs(e), 90):.0f}"
    )
    print("lead ms median", np.median([r["lead_ms"] for r in ROWS]),
          "trail ms median", np.median([r["trail_ms"] for r in ROWS]))  # fmt: skip


if __name__ == "__main__":
    main()
