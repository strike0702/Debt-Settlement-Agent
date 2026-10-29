"""Per-turn voice metrics from a normalized event timeline.

Input is a list of events (``{"t": epoch_ms, "kind": ..., ...}``) that the
driver has already mapped to target-neutral kinds, so the legacy console and
the Pipecat spike are scored by the same code:

    rep_start / rep_end (id)   scripted rep clip played into the mic
    eot                        the stack decided the rep's turn ended
    final (text)               a final rep transcript
    reply                      the server produced the reply's first sentence
    eager_eot                  Flux predicted the end of turn early (spike only)
    tts_byte                   first TTS audio byte on the server (spike only)
    audio_on / audio_off       agent audio audibly started / stopped
    filler_on                  a local filler ("One moment.") started (legacy)
    barge                      the stack decided to interrupt the agent
    stop                       agent audio stopped because of a barge
    stages (dict)              server stage timings for a turn (legacy only)

Text scoring turns spoken numbers into canonical tokens first ("one hundred
twenty dollars" and "$120" both become ``<120> dollars``; "$562.50" and "five
hundred sixty two dollars and fifty cents" both carry ``<562>`` and ``<50>``),
so WER and number recall compare meaning, not formatting. This module never touches audio, a
browser or the network.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from eval.voice.script import RepLine

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12,
    "thirteenth": 13, "fourteenth": 14, "fifteenth": 15, "sixteenth": 16,
    "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20,
    "thirtieth": 30,
}  # fmt: skip
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "january february march april may june july "
        "august september october november december".split()
    )
}
_TOKEN_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?(?:st|nd|rd|th)?%?|[a-z]+(?:'[a-z]+)?")


def _digit_tokens(tok: str) -> list[str]:
    """``$562.50`` → <562> dollars <50> cents; ``45.5%`` → <45.5> percent; ``15th`` → <15>."""
    core = re.sub(r"(st|nd|rd|th)$", "", tok.strip("$%")).replace(",", "")
    whole, _, frac = core.partition(".")
    if tok.startswith("$"):
        out = [f"<{int(whole)}>", "dollars"]
        if frac and int(frac):
            out += [f"<{int(frac.ljust(2, '0')[:2])}>", "cents"]
        return out
    out = [f"<{whole}.{frac}>" if frac.strip("0") else f"<{int(whole)}>"]
    if tok.endswith("%"):
        out.append("percent")
    return out


def _read_number_words(words: Sequence[str], i: int) -> tuple[int | None, int]:
    """Parse a run of number words from ``i``; returns (value, next index).

    "twenty twenty six" is read as two numbers (20, then 26); the caller
    joins year pairs.
    """
    total, cur, seen, j = 0, 0, False, i
    while j < len(words):
        w = words[j]
        if w in _UNITS and (
            cur % 100 == 0 or (cur % 100 >= 20 and cur % 10 == 0 and _UNITS[w] < 10)
        ):
            cur += _UNITS[w]
        elif w in _TENS and cur % 100 == 0:
            cur += _TENS[w]
        elif w == "hundred" and seen:
            cur = (cur or 1) * 100
        elif w == "thousand" and seen:
            total += (cur or 1) * 1000
            cur = 0
        elif (
            w == "and"
            and seen
            and j + 1 < len(words)
            and (words[j + 1] in _UNITS or words[j + 1] in _TENS)
            and cur % 100 == 0
            and (cur or total)
        ):
            pass  # "one hundred and five", "one thousand and five"
        else:
            break
        seen = True
        j += 1
    return (total + cur if seen else None), j


_CONTRACTIONS = {
    "we're": "we are", "you're": "you are", "they're": "they are", "i'm": "i am",
    "that's": "that is", "it's": "it is", "what's": "what is", "there's": "there is",
    "can't": "can not", "cannot": "can not", "don't": "do not", "won't": "will not",
    "we'll": "we will", "i'll": "i will", "let's": "let us",
}  # fmt: skip


def canonical_tokens(text: str) -> list[str]:
    """Lowercase word tokens with numbers as ``<n>`` and dates as ``<d:M-D>``.

    Contractions are expanded and hyphens split, so "We're" and "we are" or
    "forty-five" and "forty five" score as the same words.
    """
    lowered = text.lower().replace("-", " ").replace("\u2019", "'")
    words = _TOKEN_RE.findall(lowered)
    raw = [w for tok in words for w in _CONTRACTIONS.get(tok, tok).split()]
    out: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok in _MONTHS:
            day, nxt = _read_day(raw, i + 1)
            if day is not None:
                out.append(f"<d:{_MONTHS[tok]}-{day}>")
                i = nxt
                continue
            out.append(tok)
            i += 1
            continue
        if tok[0] in "$0123456789":
            out += _digit_tokens(tok)
            i += 1
            continue
        value, nxt = _read_number_words(raw, i)
        if value is not None:
            # "sixty five point two five" → <65.25> (digits after "point" one by one).
            digits = ""
            if nxt < len(raw) and raw[nxt] == "point":
                k = nxt + 1
                while k < len(raw) and raw[k] in _UNITS and _UNITS[raw[k]] < 10:
                    digits += str(_UNITS[raw[k]])
                    k += 1
                if digits:
                    nxt = k
            out.append(f"<{value}.{digits}>" if digits.strip("0") else f"<{value}>")
            i = nxt
            continue
        out.append(tok)
        i += 1
    return _join_years(out)


def _read_day(raw: Sequence[str], i: int) -> tuple[int | None, int]:
    if i >= len(raw):
        return None, i
    tok = raw[i]
    if tok[0].isdigit():
        return int(re.sub(r"(st|nd|rd|th)$", "", tok)), i + 1
    if tok in _ORDINALS:
        return _ORDINALS[tok], i + 1
    if tok in _TENS and i + 1 < len(raw) and raw[i + 1] in _ORDINALS:
        return _TENS[tok] + _ORDINALS[raw[i + 1]], i + 2
    return None, i


def _join_years(tokens: list[str]) -> list[str]:
    """``<20> <26>`` → ``<2026>`` (a spoken year), anything else unchanged."""
    out: list[str] = []
    for tok in tokens:
        if (
            out
            and tok.startswith("<")
            and out[-1] in ("<19>", "<20>")
            and not tok.startswith("<d:")
        ):
            n = int(tok[1:-1])
            if 10 <= n <= 99:
                out[-1] = f"<{int(out[-1][1:-1]) * 100 + n}>"
                continue
        out.append(tok)
    return out


def numbers_in(text: str) -> list[str]:
    """Number and date tokens of ``text`` in order (``<120>``, ``<d:11-15>``)."""
    return [t for t in canonical_tokens(text) if t.startswith("<")]


def wer(ref: str, hyp: str) -> float:
    """Word error rate on canonical tokens (1.0 when the hypothesis is empty)."""
    r, h = canonical_tokens(ref), canonical_tokens(hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rt in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, ht in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rt != ht))
        prev = cur
    return prev[-1] / len(r)


def number_hits(ref: str, hyp: str) -> tuple[int, int]:
    """(numbers of ``ref`` also heard in ``hyp``, numbers in ``ref``), as multisets."""
    want, got = Counter(numbers_in(ref)), Counter(numbers_in(hyp))
    return sum(min(n, got[k]) for k, n in want.items()), sum(want.values())


def first_word_lost(ref: str, hyp: str) -> bool:
    """True when the reference's first token is missing from the hypothesis's first three."""
    r, h = canonical_tokens(ref), canonical_tokens(hyp)
    return bool(r) and r[0] not in h[:3]


def _overlap(ref: str, hyp: str) -> float:
    r, h = set(canonical_tokens(ref)), set(canonical_tokens(hyp))
    return len(r & h) / len(h) if h else 0.0


# ---------------------------------------------------------------- per turn

PHANTOM_TAIL_MS = 4000
BARGE_WINDOW_MS = 1500


@dataclass
class TurnRow:
    """One scripted rep line, scored."""

    call: str
    line: str
    kind: str
    ref: str
    hyp: str = ""
    played: bool = False
    agent_talking: bool | None = None
    eot_ms: float | None = None
    eager_eot_ms: float | None = None
    final_ms: float | None = None
    reply_ms: float | None = None
    tts_byte_ms: float | None = None
    first_audio_ms: float | None = None
    any_audio_ms: float | None = None
    eot_count: int = 0
    wer: float | None = None
    num_hit: int = 0
    num_total: int = 0
    first_word_lost: bool | None = None
    barged: bool | None = None
    # Pipecat VAD pause (Phase 65): the agent's audio was paused by this line.
    paused: bool | None = None
    stop_ms: float | None = None
    stages: dict[str, Any] | None = None


def _first(events: Iterable[dict[str, Any]], kind: str, lo: float, hi: float) -> dict | None:
    return next((e for e in events if e["kind"] == kind and lo <= e["t"] < hi), None)


def _talking_at(events: Sequence[dict[str, Any]], t: float) -> bool:
    state = False
    for e in events:
        if e["t"] > t:
            break
        if e["kind"] == "audio_on":
            state = True
        elif e["kind"] in ("audio_off", "stop"):
            state = False
    return state


def score_call(
    call_id: str, lines: Sequence[RepLine], events: Sequence[dict[str, Any]]
) -> list[TurnRow]:
    """Score each scripted line of one call against its event timeline."""
    ev = sorted(events, key=lambda e: e["t"])
    starts = {e["id"]: e["t"] for e in ev if e["kind"] == "rep_start"}
    ends = {e["id"]: e["t"] for e in ev if e["kind"] == "rep_end"}
    played = [ln for ln in lines if ln.id in starts]
    rows: list[TurnRow] = []
    for idx, ln in enumerate(lines):
        row = TurnRow(call_id, ln.id, ln.kind, ln.text)
        rows.append(row)
        if ln.id not in starts:
            continue
        row.played = True
        t0, t1 = starts[ln.id], ends[ln.id]
        later = [starts[x.id] for x in played if starts[x.id] > t0]
        hi = min(later) if later else float("inf")
        row.agent_talking = _talking_at(ev, t0)
        finals = [e for e in ev if e["kind"] == "final" and t0 <= e["t"] < hi]
        row.hyp = " ".join(e["text"] for e in finals).strip()
        row.wer = wer(ln.text, row.hyp)
        row.num_hit, row.num_total = number_hits(ln.text, row.hyp)
        row.first_word_lost = first_word_lost(ln.text, row.hyp)
        row.eot_count = sum(1 for e in ev if e["kind"] == "eot" and t0 <= e["t"] < hi)
        if ln.kind == "reply":
            for name, kind in (
                ("eot_ms", "eot"), ("eager_eot_ms", "eager_eot"),
                ("final_ms", "final"), ("reply_ms", "reply"),
                ("tts_byte_ms", "tts_byte"), ("first_audio_ms", "audio_on"),
            ):  # fmt: skip
                hit = _first(ev, kind, t1 if kind == "audio_on" else t0, hi)
                if hit is not None:
                    setattr(row, name, round(hit["t"] - t1, 1))
            any_audio = [
                e["t"] for e in ev if e["kind"] in ("audio_on", "filler_on") and t1 <= e["t"] < hi
            ]
            if any_audio:
                row.any_audio_ms = round(any_audio[0] - t1, 1)
            st = _first(ev, "stages", t0, hi)
            row.stages = st.get("stages") if st else None
        else:
            barge = _first(ev, "barge", t0, t1 + BARGE_WINDOW_MS)
            row.barged = barge is not None
            row.paused = _first(ev, "pause", t0, t1 + BARGE_WINDOW_MS) is not None
            if ln.kind == "interrupt":
                stop = _first(ev, "stop", t0, hi)
                if stop is not None and row.barged:
                    row.stop_ms = round(stop["t"] - t0, 1)
    return rows


def phantom_finals(lines: Sequence[RepLine], events: Sequence[dict[str, Any]]) -> list[dict]:
    """Final transcripts no played rep line explains (agent echo, noise)."""
    windows = []
    by_id = {ln.id: ln for ln in lines}
    starts = {e["id"]: e["t"] for e in events if e["kind"] == "rep_start"}
    ends = {e["id"]: e["t"] for e in events if e["kind"] == "rep_end"}
    for lid, t0 in starts.items():
        windows.append((t0, ends[lid] + PHANTOM_TAIL_MS, by_id[lid].text))
    out = []
    for e in events:
        if e["kind"] != "final" or not e.get("text", "").strip():
            continue
        ok = any(
            lo <= e["t"] <= hi and _overlap(text, e["text"]) >= 0.25 for lo, hi, text in windows
        )
        if not ok:
            out.append(e)
    return out


def false_barges(lines: Sequence[RepLine], events: Sequence[dict[str, Any]]) -> list[dict]:
    """Barges not inside an interrupt line's window (echo, backchannel, noise)."""
    starts = {e["id"]: e["t"] for e in events if e["kind"] == "rep_start"}
    ends = {e["id"]: e["t"] for e in events if e["kind"] == "rep_end"}
    ok = [
        (starts[ln.id], ends[ln.id] + BARGE_WINDOW_MS)
        for ln in lines
        if ln.kind == "interrupt" and ln.id in starts
    ]
    return [
        e for e in events if e["kind"] == "barge" and not any(lo <= e["t"] <= hi for lo, hi in ok)
    ]


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1], 1)


def summarize(rows: Sequence[TurnRow], *, phantoms: int, false_barge_count: int) -> dict[str, Any]:
    """Headline numbers for one target run (all calls)."""
    replies = [r for r in rows if r.kind == "reply" and r.played]
    lat = [r.first_audio_ms for r in replies if r.first_audio_ms is not None]
    anyaud = [r.any_audio_ms for r in replies if r.any_audio_ms is not None]
    eot = [r.eot_ms for r in replies if r.eot_ms is not None]
    eager = [r.eager_eot_ms for r in replies if r.eager_eot_ms is not None]
    final = [r.final_ms for r in replies if r.final_ms is not None]
    heard = [r for r in rows if r.played and r.kind != "backchannel"]
    hits = sum(r.num_hit for r in heard)
    total = sum(r.num_total for r in heard)
    interrupts = [r for r in rows if r.kind == "interrupt" and r.played]
    talking_int = [r for r in interrupts if r.agent_talking]
    stops = [r.stop_ms for r in talking_int if r.stop_ms is not None]
    backs = [r for r in rows if r.kind == "backchannel" and r.played]
    return {
        "reply_turns": len(replies),
        "replied": len(lat),
        "first_audio_p50_ms": _pct(lat, 50),
        "first_audio_p95_ms": _pct(lat, 95),
        "any_audio_p50_ms": _pct(anyaud, 50),
        "eot_p50_ms": _pct(eot, 50),
        "eager_eot_p50_ms": _pct(eager, 50),
        "final_p50_ms": _pct(final, 50),
        "wer_mean": round(statistics.fmean([r.wer for r in heard if r.wer is not None]), 3)
        if heard
        else None,
        "number_recall": round(hits / total, 3) if total else None,
        "numbers": f"{hits}/{total}",
        "first_word_lost": sum(1 for r in heard if r.first_word_lost),
        "lines_scored": len(heard),
        "split_turns": sum(1 for r in replies if r.eot_count > 1),
        "phantom_finals": phantoms,
        "false_barges": false_barge_count,
        "backchannels_barged": f"{sum(1 for r in backs if r.barged)}/{len(backs)}",
        "backchannels_paused": f"{sum(1 for r in backs if r.paused)}/{len(backs)}",
        "interrupts_over_agent": f"{len(talking_int)}/{len(interrupts)}",
        "interrupts_barged": f"{sum(1 for r in talking_int if r.barged)}/{len(talking_int)}",
        "barge_stop_p50_ms": _pct(stops, 50),
        "barge_stop_max_ms": max(stops) if stops else None,
    }


def rows_as_dicts(rows: Sequence[TurnRow]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rows]
