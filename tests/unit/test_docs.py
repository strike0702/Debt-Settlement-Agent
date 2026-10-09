"""Docs and evidence hygiene (Phases 25, 25r): links, media size, frozen-pack notes.

The README promises that every number links to a committed file, so a broken
relative link or heading anchor is a correctness bug here. These tests read
only files in the repo; no network, no LLM.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LINKED_DOCS = ["README.md", "docs/DESIGN.md", "docs/eval/README.md", "docs/assets/README.md"]
MEDIA_LIMIT_BYTES = 5 * 1024 * 1024

_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
_FENCE_RE = re.compile(r"```.*?```", re.S)


def _github_slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, drop punctuation, spaces → '-'."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(md: Path) -> set[str]:
    body = _FENCE_RE.sub("", md.read_text(encoding="utf-8"))
    return {_github_slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+)$", body, re.M)}


def _relative_links(md: Path) -> list[str]:
    body = _FENCE_RE.sub("", md.read_text(encoding="utf-8"))
    return [
        link
        for link in _LINK_RE.findall(body)
        if not re.match(r"^[a-z]+:", link) and not link.startswith("#")
    ]


@pytest.mark.parametrize("doc", LINKED_DOCS)
def test_relative_links_and_anchors_resolve(doc: str) -> None:
    md = ROOT / doc
    broken: list[str] = []
    for link in _relative_links(md):
        path_part, _, anchor = link.partition("#")
        target = (md.parent / path_part).resolve()
        if not target.exists():
            broken.append(link)
        elif anchor and target.suffix == ".md" and anchor not in _anchors(target):
            broken.append(link)
    assert broken == []


def test_readme_media_within_size_budget() -> None:
    md = ROOT / "README.md"
    media = {
        (md.parent / link.partition("#")[0]).resolve()
        for link in _relative_links(md)
        if Path(link).suffix.lower() in {".gif", ".mp4", ".webm", ".png", ".jpg", ".webp"}
    }
    assert media, "README has no hero media"
    assert sum(p.stat().st_size for p in media) <= MEDIA_LIMIT_BYTES


def test_ab_results_replaced_the_pending_placeholders() -> None:
    # Phase 25r filled in the A/B result; no placeholder may come back, and both
    # docs must cite the committed summary the numbers come from.
    marker = "AB-" + "PENDING"
    for doc in ("README.md", "docs/DESIGN.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert marker not in text
        assert "ab_20261007/summary.md" in text


def test_operator_view_is_documented_as_public_by_design() -> None:
    for doc in ("README.md", "docs/DESIGN.md"):
        text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
        assert "operator view is public" in text and "synthetic" in text


def test_no_gitkeep_in_non_empty_dirs() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    stray = [
        f
        for f in tracked
        if Path(f).name == ".gitkeep"
        and any(Path(o).parent == Path(f).parent and o != f for o in tracked)
    ]
    assert stray == []


def test_frozen_transcripts_with_old_opening_carry_a_note() -> None:
    from app.agent.nlg import TEMPLATES
    from app.domain.actions import Intent

    opening_head = TEMPLATES[Intent.OPENING].split("{")[0].strip()
    for pack in sorted((ROOT / "docs/eval").glob("policy_eval_*")):
        # Transcripts are named after scenario ids (s0007_…); summary.md is not one.
        transcripts = list(pack.glob("s0*.md"))
        stale = [p.name for p in transcripts if opening_head not in p.read_text(encoding="utf-8")]
        if stale:
            note = pack / "NOTE.md"
            assert note.exists(), f"{pack.name}: {stale} predate the opening line, no NOTE.md"
            assert "opening" in note.read_text(encoding="utf-8")


def test_keepwarm_workflow_pings_healthz_every_ten_minutes() -> None:
    # The owner enabled the schedule on 2026-10-07; it must stay a cheap /healthz
    # ping (no LLM, engine or DB) and keep a manual trigger.
    wf = yaml.safe_load((ROOT / ".github/workflows/keepwarm.yml").read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))
    assert set(triggers) == {"workflow_dispatch", "schedule"}
    assert triggers["schedule"] == [{"cron": "*/10 * * * *"}]
    text = (ROOT / ".github/workflows/keepwarm.yml").read_text(encoding="utf-8")
    assert "/healthz" in text
