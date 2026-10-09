#!/usr/bin/env python3
"""Build ``config/nlg_bank.json``: guard-checked NLG templates for ``NLG_MODE=bank``.

1. Collect the (intent, sorted placeholder ids) keys the policy actually emits
   for LLM-phrased intents (not in ``TEMPLATE_ONLY_INTENTS``, no
   ``template_override``) by running offline oracle-NLU / template-NLG sim calls,
   plus ``LIVE_ONLY_KEYS`` that only live NLU reaches (hedged read-backs,
   commitment refusals).
2. Ask the ``nlg`` role for ``--per-key`` templates per key, with the same
   prompt the live path uses (``app.llm.prompts.nlg_messages``).
3. Split each reply into one candidate per line. Keep a candidate only if
   ``template_guard`` passes with that key's ids and ``bank_required`` ids,
   the text has no commitment / boundary phrase (``rendered_guard`` on the
   placeholder-free text), and it does not misread a fact (``_MISREAD_RE``:
   the offer total spoken as a per-payment amount). Duplicates are dropped;
   at most ``--per-key`` are kept.

``--acts`` (Phase 24b) builds only the H3 act keys and merges them into the
existing bank: ``ACK`` for every ``ACK_TEMPLATES`` id set (prompt
``app.llm.prompts.act_messages``, same acceptance checks) and
``ANSWER:<topic>`` paraphrases of each ``ANSWER_POINTS`` talking point (no
placeholders; the default talking point is always kept as the first entry).

Both builds merge into the existing ``--out`` file (Phase 47): an entry marked
``"reviewed": true`` (hand-written or hand-edited copy, e.g. the Phase 33
READ_BACK / CLARIFY / COUNTER lines) is kept as is and its key is not sent to
the LLM; a full build leaves the act keys alone, ``--acts`` leaves the rest
alone, and top-level notes in the file are kept. Drop the flag from an entry
to let the next build regenerate it.

The live call never sees this script; it only reads the JSON
(``app.agent.nlg_bank``). Usage: ``uv run python scripts/build_template_bank.py
[--profile demo] [--per-key 8] [--seeds 20] [--acts]``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.agent.orchestrator as orch_mod  # noqa: E402
from app.agent.acts import ANSWER_POINTS  # noqa: E402
from app.agent.guards import rendered_guard, template_guard  # noqa: E402
from app.agent.nlg import (  # noqa: E402
    ACK_BANK_INTENT,
    ACK_TEMPLATES,
    TEMPLATE_ONLY_INTENTS,
    answer_bank_intent,
)
from app.agent.nlg_bank import BankKey, action_placeholder_ids, bank_key  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.domain.actions import Action, Intent  # noqa: E402
from app.llm.client import FakeLLM, make_client  # noqa: E402
from app.llm.prompts import act_messages, nlg_messages  # noqa: E402

OUT_PATH = ROOT / "config" / "nlg_bank.json"
# Offline, so latency does not matter: gpt-oss reasoning tokens count against
# max_tokens, and the live cap (400) often leaves an empty template.
_MAX_TOKENS = 2000

# Keys live NLU reaches but the oracle sim does not (it never hedges or pressures
# with a first commitment demand that is refused rather than escalated).
LIVE_ONLY_KEYS: tuple[BankKey, ...] = (
    bank_key(Intent.READ_BACK.value, ["field_label", "readback_value"]),
    bank_key(Intent.REFUSE_COMMIT.value, []),
    bank_key(Intent.REFUSE_PRIVATE.value, []),
    bank_key(Intent.COUNTER.value, ["counter_pct", "offer_total"]),
)

# Ids a bank template must speak. Stricter than an action's ``required``: an enum
# read-back carries its value in a text slot, which runtime does not require.
# Schedule detail (``payment_level_*``, ``last_payment``) stays optional.
_OPTIONAL_RE = re.compile(r"^(payment_level_\d+|last_payment)$")


def bank_required(placeholders: tuple[str, ...]) -> set[str]:
    """Placeholder ids every bank template for this key must contain."""
    return {p for p in placeholders if not _OPTIONAL_RE.match(p)}


# Guard-clean but false: {offer_total} is the whole settlement, never one payment
# ("four payments of $2,000"); {counter_pct} is a share of the balance.
_MISREAD_RE = re.compile(
    r"(payments?|installments?)\s+of\s+\{offer_total\}|\{counter_pct\}\s+payment",
    re.IGNORECASE,
)


def template_ok(text: str, placeholders: tuple[str, ...]) -> bool:
    """Bank acceptance: template_guard, no commitment/boundary wording, no misread."""
    if not text or "\n" in text or _MISREAD_RE.search(text):
        return False
    if not template_guard(text, set(placeholders), bank_required(placeholders)).ok:
        return False
    plain = re.sub(r"\{[^}]+\}", "that", text)
    return rendered_guard(plain, {}, set(), set()).ok


async def collect_keys(seeds: int) -> list[BankKey]:
    """Keys of LLM-phrased actions across offline oracle sim calls."""
    from eval.run_eval import _build_settings, run_one_scenario
    from sim.personas import PERSONAS
    from sim.scenarios import STRATA, generate_one

    seen: set[BankKey] = set(LIVE_ONLY_KEYS)
    original = orch_mod.render_action

    def record(action: Action, *args: Any, **kwargs: Any) -> list[str]:
        if action.intent not in TEMPLATE_ONLY_INTENTS and action.template_override is None:
            seen.add(bank_key(action.intent.value, action_placeholder_ids(action)))
        return original(action, *args, **kwargs)

    settings = _build_settings(profile="offline", nlg="template", nlu="oracle")
    orch_mod.render_action = record  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as d:
            for seed in range(seeds):
                for persona in PERSONAS:
                    for stratum in STRATA:
                        try:
                            sc = generate_one(persona, stratum, seed=seed)
                        except RuntimeError:  # stratum not sampleable at this seed
                            continue
                        await run_one_scenario(
                            sc,
                            settings=settings,
                            llm=FakeLLM(),
                            sim_phrasing="template",
                            audit_dir=Path(d),
                        )
    finally:
        orch_mod.render_action = original  # type: ignore[assignment]
    return sorted(seen)


_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def candidates(text: str) -> list[str]:
    """Template candidates in one reply: one per non-empty line, fences/bullets stripped.

    Asked for "variant k of n", the model sometimes lists several variants.
    """
    out = []
    for line in text.strip().split("\n"):
        line = _BULLET_RE.sub("", line).strip().strip('"').strip()
        if line and not line.startswith("```"):
            out.append(line)
    return out


def is_act_entry(entry: dict[str, Any]) -> bool:
    """True for the H3 act keys (``ACK``, ``ANSWER:<topic>``) that ``--acts`` owns."""
    return entry["intent"] == ACK_BANK_INTENT or entry["intent"].startswith("ANSWER:")


def reviewed_keys(bank: dict[str, Any]) -> set[BankKey]:
    """Keys of entries marked ``reviewed``: never regenerated, never overwritten."""
    return {
        bank_key(e["intent"], e["placeholders"])
        for e in bank.get("entries", [])
        if e.get("reviewed") is True
    }


def merge_entries(
    bank: dict[str, Any], fresh: list[dict[str, Any]], *, acts: bool
) -> list[dict[str, Any]]:
    """Existing entries outside this build's scope or marked reviewed, then ``fresh``.

    Scope is the act keys for ``--acts`` and every other key for a full build.
    A fresh entry whose key is reviewed is dropped (the hand copy wins).
    """
    keep = [
        e for e in bank.get("entries", []) if is_act_entry(e) != acts or e.get("reviewed") is True
    ]
    held = {bank_key(e["intent"], e["placeholders"]) for e in keep}
    return keep + [e for e in fresh if bank_key(e["intent"], e["placeholders"]) not in held]


def read_bank(path: Path) -> dict[str, Any]:
    """The existing bank at ``path``, or ``{}`` when there is none yet."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


async def generate(
    keys: list[BankKey], *, profile: str, per_key: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Ask the nlg role ``per_key`` times per key; return bank entries + stats."""
    settings = get_settings().model_copy(update={"llm_profile": profile, "llm_cache": False})
    models: dict[str, int] = {}

    def on_call(meta: dict[str, Any]) -> None:
        if not meta.get("error"):
            m = f"{meta['provider']}/{meta['model']}"
            models[m] = models.get(m, 0) + 1

    llm = make_client(settings, on_call=on_call)
    stats = {"asked": 0, "kept": 0, "rejected": 0, "duplicate": 0}
    entries: list[dict[str, Any]] = []
    try:
        for intent_value, placeholders in keys:
            base = nlg_messages(Intent(intent_value), list(placeholders), "")
            kept: list[str] = []
            for k in range(per_key):
                msgs = [
                    *base,
                    {
                        "role": "user",
                        "content": f"Variant {k + 1} of {per_key}: vary the wording.",
                    },
                ]
                stats["asked"] += 1
                reply = await llm.chat_text("nlg", msgs, _MAX_TOKENS)
                for text in candidates(reply):
                    if not template_ok(text, placeholders):
                        stats["rejected"] += 1
                    elif text in kept:
                        stats["duplicate"] += 1
                    elif len(kept) < per_key:
                        kept.append(text)
            stats["kept"] += len(kept)
            print(f"{intent_value:<18} {','.join(placeholders) or '-':<60} kept {len(kept)}")
            entries.append(
                {
                    "intent": intent_value,
                    "placeholders": list(placeholders),
                    "required": sorted(bank_required(placeholders)),
                    "templates": kept,
                }
            )
    finally:
        await llm.aclose()
    return entries, {**stats, **{f"model:{m}": n for m, n in models.items()}}


# Paraphrases that add a promise or a time claim the talking point never made.
_ANSWER_EXTRA_RE = re.compile(
    r"\b(?:guarantee|promise|today|tomorrow|week|hours?|days?|soon|shortly|approved)\b",
    re.IGNORECASE,
)


async def generate_acts(
    *, profile: str, per_key: int, skip: set[BankKey] | None = None
) -> list[dict[str, Any]]:
    """Bank entries for the H3 ``ACK`` / ``ANSWER:<topic>`` keys (``skip``: reviewed keys)."""
    settings = get_settings().model_copy(update={"llm_profile": profile, "llm_cache": False})
    llm = make_client(settings)
    entries: list[dict[str, Any]] = []
    jobs: list[tuple[str, tuple[str, ...], list[dict[str, str]], str | None]] = [
        (ACK_BANK_INTENT, ids, act_messages("ack", list(ids)), ACK_TEMPLATES[ids])
        for ids in ACK_TEMPLATES
    ] + [
        (answer_bank_intent(topic), (), act_messages("answer", [], talking_point=text), text)
        for topic, text in ANSWER_POINTS.items()
    ]
    jobs = [j for j in jobs if bank_key(j[0], j[1]) not in (skip or set())]
    try:
        for intent_key, ids, base, default in jobs:
            kept = [default] if default and template_ok(default, ids) else []
            for k in range(per_key * 2):
                if len(kept) >= per_key:
                    break
                msgs = [
                    *base,
                    {"role": "user", "content": f"Variant {k + 1}: vary the wording."},
                ]
                reply = await llm.chat_text("nlg", msgs, _MAX_TOKENS)
                for text in candidates(reply):
                    if intent_key.startswith("ANSWER:") and (
                        _ANSWER_EXTRA_RE.search(text) and not _ANSWER_EXTRA_RE.search(default or "")
                    ):
                        continue
                    if template_ok(text, ids) and text not in kept and len(kept) < per_key:
                        kept.append(text)
            print(f"{intent_key:<24} {','.join(ids) or '-':<60} kept {len(kept)}")
            entries.append(
                {
                    "intent": intent_key,
                    "placeholders": list(ids),
                    "required": sorted(bank_required(ids)),
                    "templates": kept,
                }
            )
    finally:
        await llm.aclose()
    return entries


def merge_act_entries(bank: dict[str, Any], acts: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the unreviewed ``ACK`` / ``ANSWER:*`` entries in ``bank`` with ``acts``."""
    entries = merge_entries(bank, acts, acts=True)
    return {**bank, "entries": entries, "acts_generated": date.today().isoformat()}


def merge_full_build(
    bank: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    profile: str,
    per_key: int,
    stats: dict[str, int],
) -> dict[str, Any]:
    """Replace the unreviewed non-act entries in ``bank``; keep its notes and act keys."""
    return {
        **bank,
        "generated": date.today().isoformat(),
        "profile": profile,
        "per_key": per_key,
        "stats": stats,
        "entries": merge_entries(bank, entries, acts=False),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default="demo")
    ap.add_argument("--per-key", type=int, default=8)
    ap.add_argument("--seeds", type=int, default=20, help="sim seeds for key collection")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--acts", action="store_true", help="only (re)build the H3 act keys")
    args = ap.parse_args(argv)

    bank = read_bank(args.out)
    skip = reviewed_keys(bank)
    if args.acts:
        acts = asyncio.run(generate_acts(profile=args.profile, per_key=args.per_key, skip=skip))
        args.out.write_text(
            json.dumps(merge_act_entries(bank, acts), indent=2) + "\n", encoding="utf-8"
        )
        return 0

    keys = [k for k in asyncio.run(collect_keys(args.seeds)) if k not in skip]
    entries, stats = asyncio.run(generate(keys, profile=args.profile, per_key=args.per_key))
    merged = merge_full_build(
        bank, entries, profile=args.profile, per_key=args.per_key, stats=stats
    )
    args.out.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
