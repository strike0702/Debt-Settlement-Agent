"""FastAPI serves the built web UI (``web/dist``): SPA fallback and cache headers.

Uses a fake dist directory, so these tests do not need ``npm run build``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.domain.scenario import rep_card_suggestions
from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog
from tests.wsutil import offline_settings


def _client(tmp_path: Path, dist: Path) -> TestClient:
    app = create_app(
        settings=offline_settings(),
        llm=FakeLLM(),
        audit=AuditLog(tmp_path / "a.db"),
        web_dist=dist,
    )
    return TestClient(app)


def _fake_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>console</title>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    return dist


def test_index_and_spa_fallback_are_no_cache(tmp_path: Path) -> None:
    with _client(tmp_path, _fake_dist(tmp_path)) as c:
        for path in ("/", "/call/xyz", "/some/deep/route"):
            r = c.get(path)
            assert r.status_code == 200, path
            assert "<title>console</title>" in r.text
            assert r.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_cacheable(tmp_path: Path) -> None:
    with _client(tmp_path, _fake_dist(tmp_path)) as c:
        r = c.get("/assets/index-abc123.js")
        assert r.status_code == 200
        assert "immutable" in r.headers["cache-control"]
        assert "max-age=31536000" in r.headers["cache-control"]
        assert c.get("/assets/missing.js").status_code == 404
        fav = c.get("/favicon.svg")
        assert fav.text == "<svg/>" and fav.headers["cache-control"] == "no-cache"


def test_api_paths_are_not_swallowed_by_the_fallback(tmp_path: Path) -> None:
    with _client(tmp_path, _fake_dist(tmp_path)) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.get("/scenarios/nope").status_code == 404
        assert c.get("/calls/x/unknown").status_code == 404
        assert c.get("/metrics/nope").status_code == 404
        assert isinstance(c.get("/scenarios").json(), list)


def test_unbuilt_dist_says_how_to_build(tmp_path: Path) -> None:
    with _client(tmp_path, tmp_path / "missing") as c:
        r = c.get("/")
        assert r.status_code == 503
        assert "npm run build" in r.text


def test_path_traversal_stays_inside_dist(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("nope")
    with _client(tmp_path, _fake_dist(tmp_path)) as c:
        r = c.get("/..%2Fsecret.txt")
        assert "nope" not in r.text


def test_scenarios_carry_rep_card_suggestions(tmp_path: Path) -> None:
    """[23a.2] every curated scenario offers suggested rep replies from its rep card."""
    with _client(tmp_path, _fake_dist(tmp_path)) as c:
        rows = c.get("/scenarios").json()
    assert {r["id"] for r in rows} >= {"easy_deal", "no_space", "rescue_escalate"}
    for row in rows:
        assert len(row["suggested"]) >= 3, row["id"]
        assert not any(ch.isdigit() for line in row["suggested"] for ch in line), row["id"]


def test_rep_card_suggestions_answer_read_backs(tmp_path: Path) -> None:
    """[P23b] every card offers a reply that confirms a pending read-back.

    The confirm line takes the deterministic fast path, and with no read-back
    pending it is not a bare acknowledgement, so it cannot be read as an accept.
    """
    from app.agent.nlu import repair_stance, try_fast_readback

    with _client(tmp_path, _fake_dist(tmp_path)) as c:
        rows = c.get("/scenarios").json()
    for row in rows:
        confirms = [
            ln for ln in row["suggested"]
            if (fast := try_fast_readback(ln, "max_payments")) is not None
            and fast.readback_response == "confirm"
        ]
        assert confirms, row["id"]
        assert all(repair_stance("other", ln) != "accept" for ln in confirms), row["id"]


def test_rep_card_suggestions_parses_only_its_section() -> None:
    md = (
        "# Card\n\n## Rules\n\n- not me\n\n## Suggested replies\n\nIntro.\n\n"
        "- one\n- two\n\n## Notes\n- no\n"
    )
    assert rep_card_suggestions(md) == ["one", "two"]
    assert rep_card_suggestions("# none") == []
