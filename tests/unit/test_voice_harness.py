"""Voice harness (eval/voice) scoring, offline: number tokens, WER, per-turn timing,
phantom lines, false barges, the legacy and spike event mapping, and clip trimming.
No browser, server or Deepgram call."""

from __future__ import annotations

import array

import pytest

from eval.voice import driver, fixtures, metrics
from eval.voice.script import CALLS, RepLine, all_lines


@pytest.mark.parametrize(
    ("spoken", "written"),
    [
        ("at least one hundred twenty dollars each", "at least $120 each"),
        ("forty-five percent of the balance", "45% of the balance"),
        ("forty five point five percent", "45.5%"),
        ("on November fifteenth", "on November 15th"),
        ("December first, twenty twenty-six", "December 1st, 2026"),
        ("five hundred sixty two dollars fifty cents", "$562.50"),
        ("one thousand two hundred and fifty", "1,250"),
        ("We're looking for it", "we are looking for it"),
    ],
)
def test_spoken_and_written_numbers_match(spoken: str, written: str) -> None:
    assert metrics.canonical_tokens(spoken) == metrics.canonical_tokens(written)


def test_number_hits_and_first_word() -> None:
    ref = "No, that's everything. Forty-two percent."
    assert metrics.number_hits(ref, "that's everything, 42%") == (1, 1)
    assert metrics.number_hits(ref, "that's everything, 20%") == (0, 1)
    assert metrics.first_word_lost(ref, "that's everything")
    assert not metrics.first_word_lost(ref, "no that's everything")
    assert metrics.wer("one two three", "") == 1.0
    assert metrics.wer("Correct.", "correct") == 0.0


def _line_events(line_id: str, t0: float, dur: float) -> list[dict]:
    return [
        {"t": t0, "kind": "rep_start", "id": line_id},
        {"t": t0 + dur, "kind": "rep_end", "id": line_id},
    ]


def test_score_call_reply_timings_and_barges() -> None:
    lines = (
        RepLine("bc", "Mm-hmm.", kind="backchannel"),
        RepLine("r1", "We are looking for forty-five percent."),
        RepLine("int", "Hold on, forty-two percent.", kind="interrupt"),
    )
    ev = [
        {"t": 0, "kind": "audio_on"},
        *_line_events("bc", 1000, 300),
        {"t": 1200, "kind": "barge"},  # a barge on the backchannel: false
        {"t": 1250, "kind": "stop"},
        *_line_events("r1", 5000, 2000),
        {"t": 7700, "kind": "eot"},
        {"t": 7900, "kind": "final", "text": "looking for 45%"},
        {"t": 8500, "kind": "reply"},
        {"t": 9000, "kind": "audio_on"},
        *_line_events("int", 10000, 1500),
        {"t": 10600, "kind": "barge"},
        {"t": 10700, "kind": "stop"},
        {"t": 12500, "kind": "final", "text": "hold on 42%"},
        {"t": 20000, "kind": "final", "text": "payment payment payment"},  # echo
    ]
    rows = {r.line: r for r in metrics.score_call("c", lines, ev)}
    r1 = rows["r1"]
    assert (r1.eot_ms, r1.final_ms, r1.reply_ms, r1.first_audio_ms) == (700, 900, 1500, 2000)
    assert r1.num_hit == r1.num_total == 1 and r1.first_word_lost
    assert rows["bc"].barged and rows["bc"].agent_talking
    assert rows["int"].barged and rows["int"].stop_ms == 700
    assert len(metrics.false_barges(lines, ev)) == 1
    phantoms = metrics.phantom_finals(lines, ev)
    assert [p["text"] for p in phantoms] == ["payment payment payment"]
    s = metrics.summarize(list(rows.values()), phantoms=1, false_barge_count=1)
    assert s["first_audio_p50_ms"] == 2000 and s["barge_stop_p50_ms"] == 700
    assert s["backchannels_barged"] == "1/1" and s["interrupts_barged"] == "1/1"


def test_legacy_mapping_skips_fillers_and_counts_wav_as_end_of_turn() -> None:
    run = driver.CallRun("c", "legacy-server", "inject")
    run.page = [
        {"t": 1, "kind": "ws_out_wav", "bytes": 10},
        {"t": 2, "kind": "ws_in", "type": "transcript", "role": "creditor", "text": "Correct"},
        {"t": 3, "kind": "tts_start", "text": "One moment."},
        {"t": 4, "kind": "ws_in", "type": "say", "id": "x", "text": "Thanks."},
        {"t": 5, "kind": "tts_start", "text": "Thanks."},
        {"t": 6, "kind": "ws_out", "type": "barge_in", "spoken_ids": []},
        {"t": 7, "kind": "tts_cancel"},
    ]
    kinds = [e["kind"] for e in driver.normalize(run)]
    assert kinds == ["eot", "final", "filler_on", "reply", "audio_on", "barge", "stop"]


def test_spike_mapping_counts_interruptions_only_while_the_bot_speaks() -> None:
    run = driver.CallRun("c", "spike", "inject")
    run.server = [
        {"t": 100, "kind": "start_of_turn"},
        {"t": 100, "kind": "interruption"},  # bot silent: not a barge
        {"t": 900, "kind": "end_of_turn"},
        {"t": 900, "kind": "final", "text": "hi"},
        {"t": 1000, "kind": "bot_started"},
        {"t": 1500, "kind": "interruption"},  # bot speaking: a barge
        {"t": 1510, "kind": "bot_stopped"},
    ]
    run.page = [{"t": 1050, "kind": "agent_audio_on"}, {"t": 1600, "kind": "agent_audio_off"}]
    kinds = [e["kind"] for e in driver.normalize(run)]
    assert kinds.count("barge") == 1
    assert "stop" in kinds


def test_trim_keeps_soft_onset_and_drops_silence() -> None:
    rate = 16000
    silence = [0] * rate  # 1 s
    soft = [200 if i % 2 else -200 for i in range(rate // 10)]  # quiet onset, 100 ms
    loud = [5000 if i % 2 else -5000 for i in range(rate // 2)]
    pcm = array.array("h", silence + soft + loud + silence).tobytes()
    out = array.array("h", fixtures.trim(pcm, rate=rate))
    assert len(out) < len(pcm) // 2 // 2 + rate  # most silence gone
    assert 200 in out  # the soft onset survived


def test_scripts_cover_the_phase_cases_and_fixtures_exist() -> None:
    kinds = {ln.kind for ln in all_lines()}
    assert kinds == {"reply", "backchannel", "interrupt"}
    assert any(ln.parts for ln in all_lines())  # a mid-sentence pause
    texts = " ".join(ln.text for ln in all_lines())
    for needle in ("one hundred twenty dollars", "forty-five percent", "November fifteenth"):
        assert needle in texts
    ids = [ln.id for ln in all_lines()]
    assert len(ids) == len(set(ids))
    for call in CALLS:
        for ln in call.lines:
            assert (driver.FIXTURES / ln.wav_name).exists(), ln.wav_name
