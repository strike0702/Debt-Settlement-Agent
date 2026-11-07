"""Suggested rep replies that state a scenario's payment tiers and level limit.

A person playing the rep reads these aloud (or clicks them), so they use number
words the way a rep would say them ("The first two payments must be at least one
hundred ninety dollars."). They are built from the scenario's true creditor rules
(``sim.json`` ``rules``) and merged into the rep card's suggested replies by
``GET /scenarios``. Creditor terms only, nothing PRIVATE.

This only writes text. A clicked line goes through NLU like any typed line; it
never touches the belief directly.
"""

from __future__ import annotations

from typing import Any

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_DEFAULT_LEVELS = 2  # the agent's assumed level limit (``fields.max_segments``)


def number_words(n: int) -> str:
    """``0 <= n < 1,000,000`` in words: 190 → "one hundred ninety", 25 → "twenty-five"."""
    if not 0 <= n < 1_000_000:
        raise ValueError(f"out of range: {n}")
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {number_words(rest)}" if rest else "")
    thousands, rest = divmod(n, 1000)
    return f"{number_words(thousands)} thousand" + (f" {number_words(rest)}" if rest else "")


def money_words(cents: int) -> str:
    """19000 → "one hundred ninety dollars"; 2550 → "twenty-five dollars and fifty cents"."""
    dollars, rest = divmod(cents, 100)
    out = f"{number_words(dollars)} dollar{'s' if dollars != 1 else ''}"
    return out + (f" and {number_words(rest)} cent{'s' if rest != 1 else ''}" if rest else "")


def _tier_line(frm: int, to: int, cents: int) -> str:
    floor = money_words(cents)
    if frm == 1:
        if to == 1:
            return f"The first payment must be at least {floor}."
        return f"The first {number_words(to)} payments must be at least {floor}."
    if to == frm:
        return f"Payment {number_words(frm)} must be at least {floor}."
    return f"Payments {number_words(frm)} to {number_words(to)} must be at least {floor}."


def rule_reply_lines(rules: dict[str, Any]) -> list[str]:
    """Lines stating the tiers (count, one per tier, "That's all the tiers.") and the level limit.

    Empty unless the creditor has tiers or a level limit other than the agent's default.
    Each tier runs to the payment before the next one, the last to max payments.
    """
    tiers = sorted((int(a), int(b)) for a, b in rules.get("min_payment_tiers") or [])
    levels = rules.get("max_segments")
    if not tiers and levels in (None, _DEFAULT_LEVELS):
        return []
    out: list[str] = []
    if tiers:
        n = len(tiers)
        count = "is one payment tier" if n == 1 else f"are {number_words(n)} payment tiers"
        out.append(f"There {count}.")
        last = int(rules.get("max_payments") or tiers[-1][0])
        for i, (frm, cents) in enumerate(tiers):
            to = tiers[i + 1][0] - 1 if i + 1 < n else max(last, frm)
            out.append(_tier_line(frm, to, cents))
        out.append("That's all the tiers.")
    if levels is not None:
        k = int(levels)
        out.append(
            "Only one payment level." if k == 1 else f"At most {number_words(k)} payment levels."
        )
    return out


def merge_suggestions(card: list[str], rules: dict[str, Any] | None) -> list[str]:
    """Card lines with the rule lines inserted after the card's last tier line (else its first)."""
    extra = [line for line in rule_reply_lines(rules or {}) if line not in card]
    if not extra:
        return card
    at = max((i for i, line in enumerate(card) if "tier" in line.lower()), default=0) + 1
    return card[:at] + extra + card[at:]
