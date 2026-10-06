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

The live call never sees this script; it only reads the JSON
(``app.agent.nlg_bank``). Usage: ``uv run python scripts/build_template_bank.py
[--profile demo] [--per-key 8] [--seeds 20]``.
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
from app.agent.guards import rendered_guard, template_guard  # noqa: E402
from app.agent.nlg import TEMPLATE_ONLY_INTENTS  # noqa: E402
from app.agent.nlg_bank import BankKey, action_placeholder_ids, bank_key  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.domain.actions import Action, Intent  # noqa: E402
from app.llm.client import FakeLLM, make_client  # noqa: E402
from app.llm.prompts import nlg_messages  # noqa: E402

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", default="demo")
    ap.add_argument("--per-key", type=int, default=8)
    ap.add_argument("--seeds", type=int, default=20, help="sim seeds for key collection")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)

    keys = asyncio.run(collect_keys(args.seeds))
    entries, stats = asyncio.run(generate(keys, profile=args.profile, per_key=args.per_key))
    bank = {
        "generated": date.today().isoformat(),
        "profile": args.profile,
        "per_key": args.per_key,
        "stats": stats,
        "entries": entries,
    }
    args.out.write_text(json.dumps(bank, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
