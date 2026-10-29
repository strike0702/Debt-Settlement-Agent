"""Phase 63 Pipecat voice path: heard tracking, rollback, eager turns, barge rule, budget, bridge.

Fake call / fake orchestrator / no Deepgram: the real orchestrator runs on the
oracle NLU and template NLG where the rollback matters.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test

from app.agent.orchestrator import Orchestrator, Utterance
from app.config import Settings
from app.llm.client import FakeLLM
from app.main import create_app
from app.store.audit import AuditLog
from app.voice import ws as call_ws
from app.voice.pipeline import processors
from app.voice.pipeline.budget import VoiceBudget
from app.voice.pipeline.heard import HeardTracker
from app.voice.pipeline.processors import OrchestratorProcessor, SilenceFill
from app.voice.pipeline.rules import is_backchannel, is_barge
from app.voice.pipeline.session import VoiceSession
from app.voice.pipeline.turns import VoiceTurns
from tests.unit.test_unheard_barge import _ASK, _ladder, _orch, _to_first_counter


class FakeCall:
    """VoiceCall over a real orchestrator, recording what reached the UI."""

    def __init__(self, orch: Any) -> None:
        self.orch = orch
        self.shown: list[str] = []
        self.emitted: list[Utterance] = []
        self.rows: list[tuple[str, dict[str, Any]]] = []
        self.states: list[tuple[str, str]] = []

    async def show_creditor(self, text: str) -> None:
        self.shown.append(text)

    async def emit(self, utt: Utterance) -> None:
        self.emitted.append(utt)

    async def ack(self, ids: list[str]) -> None:
        await self.orch.on_sentence_done(ids)

    async def barge(self, spoken: list[str]) -> None:
        await self.orch.on_barge_in(spoken)

    async def nlu_unavailable(self, e: Exception) -> None:
        raise AssertionError(e)

    def audit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.rows.append((event_type, payload))

    async def voice_state(self, state: str, text: str) -> None:
        self.states.append((state, text))


async def _speak_and_interrupt(orch: Orchestrator, utt: Utterance, played: int) -> None:
    """Queue ``utt`` as the pipeline would, play ``played`` sentences, then barge."""
    tracker, turns = HeardTracker(), VoiceTurns(FakeCall(orch), eager=False)
    for sid, _ in utt.sentences:
        tracker.queued(sid)
    for sid, _ in utt.sentences[:played]:
        assert tracker.played(sid)
        await turns.on_played(sid)
    await turns.on_barge(tracker.interrupt().heard)


# ---------------------------------------------------------------- heard / rollback


async def test_barge_before_any_audio_rolls_the_turn_back(tmp_path: Path) -> None:
    orch, dropped = _orch(tmp_path)
    await _to_first_counter(orch)
    before = _ladder(orch)
    hold = await orch.on_creditor_text("Seventy percent.", oracle=_ASK)
    await _speak_and_interrupt(orch, hold, played=0)
    assert _ladder(orch) == before
    assert dropped == [[sid for sid, _ in hold.sentences]]


async def test_barge_after_sentence_one_keeps_sentence_one_heard(tmp_path: Path) -> None:
    orch, dropped = _orch(tmp_path)
    anchor = await _to_first_counter(orch)
    hold = await orch.on_creditor_text("Seventy percent.", oracle=_ASK)
    assert len(hold.sentences) >= 2
    await _speak_and_interrupt(orch, hold, played=1)
    assert orch.session.neg.hold_stage == 1 and orch.session.neg.counters_offered == [anchor]
    assert dropped == [[sid for sid, _ in hold.sentences[1:]]]
    spoken = {t.sentence_id: t.spoken for t in orch.session.history if t.sentence_id}
    assert spoken[hold.sentences[0][0]] is True
    assert spoken[hold.sentences[1][0]] is False


async def test_barge_after_everything_played_drops_nothing(tmp_path: Path) -> None:
    orch, dropped = _orch(tmp_path)
    await _to_first_counter(orch)
    hold = await orch.on_creditor_text("Seventy percent.", oracle=_ASK)
    await _speak_and_interrupt(orch, hold, played=len(hold.sentences))
    assert orch.session.pending is None and dropped == []
    assert orch.session.neg.hold_stage == 1


async def test_withdrawn_eager_turn_rolls_back_without_merging_its_text(tmp_path: Path) -> None:
    orch, _ = _orch(tmp_path)
    await _to_first_counter(orch)
    before = _ladder(orch)
    await orch.on_creditor_text("Seventy percent.", oracle=_ASK)
    assert await orch.withdraw_unheard_turn() is True
    assert _ladder(orch) == before
    u = await orch.on_creditor_text("Seventy percent, final.", oracle=_ASK)
    assert u.action.reason == "hold"
    texts = [e["payload"]["text"] for e in orch.audit.for_call(orch.session.call_id)
             if e["type"] == "utterance"]  # fmt: skip
    assert texts[-1] == "Seventy percent, final."


# ---------------------------------------------------------------- eager turns


class CountingOrch:
    """Fake orchestrator: one Utterance per turn, records withdrawals."""

    def __init__(self) -> None:
        self.turns: list[str] = []
        self.withdrawn = 0

    async def on_creditor_text(self, text: str) -> Utterance:
        await asyncio.sleep(0)
        self.turns.append(text)
        return Utterance(sentences=[(f"s{len(self.turns)}", "Ok.")], action=None)  # type: ignore[arg-type]

    async def merge_into_nlu(self, text: str) -> Utterance | None:
        return None  # its NLU already finished: nothing to merge into

    async def withdraw_unheard_turn(self) -> bool:
        self.withdrawn += 1
        return True


async def test_eager_turn_is_reused_when_the_final_says_the_same_words() -> None:
    call = FakeCall(CountingOrch())
    turns = VoiceTurns(call, eager=True)
    turns.on_eager("Yes that works")
    await turns.on_final("Yes, that works.")
    assert call.orch.turns == ["Yes that works"] and call.orch.withdrawn == 0
    assert len(call.emitted) == 1


async def test_resumed_eager_turn_is_withdrawn_and_the_final_runs() -> None:
    call = FakeCall(CountingOrch())
    turns = VoiceTurns(call, eager=True)
    turns.on_eager("Yes that works")
    turns.on_resumed()
    await turns.on_final("Yes that works for six payments.")
    assert call.orch.turns == ["Yes that works", "Yes that works for six payments."]
    assert call.orch.withdrawn == 1
    assert [u.sentences[0][0] for u in call.emitted] == ["s2"]


# ---------------------------------------------------------------- barge rule / processor


def test_barge_rule() -> None:
    assert is_backchannel("Mm-hmm.") and is_backchannel("Okay, right.")
    assert not is_backchannel("Correct.")
    assert not is_barge("Okay.") and not is_barge("No.")
    assert is_barge("No, that's wrong.")


class FakeTurns:
    def __init__(self) -> None:
        self.call = FakeCall(None)
        self.finals: list[str] = []
        self.barges: list[list[str]] = []

    async def on_final(self, text: str) -> None:
        self.finals.append(text)

    async def on_barge(self, heard: list[str]) -> None:
        self.barges.append(heard)


async def test_processor_barges_only_on_real_speech_while_the_agent_talks() -> None:
    turns, tracker = FakeTurns(), HeardTracker()
    tracker.queued("a1")
    tracker.queued("a2")
    tracker.played("a1")
    proc = OrchestratorProcessor(turns, tracker)  # type: ignore[arg-type]
    down, _ = await run_test(
        proc,
        frames_to_send=[
            BotStartedSpeakingFrame(),
            TranscriptionFrame("Okay.", "rep", "t"),  # backchannel: ignored
            InterimTranscriptionFrame("mm okay", "rep", "t"),  # no barge
            InterimTranscriptionFrame("No wait, that is wrong", "rep", "t"),  # barge
            TranscriptionFrame("No wait, that is wrong.", "rep", "t"),
        ],
        expected_down_frames=[BotStartedSpeakingFrame, InterruptionFrame],
    )
    await asyncio.sleep(0.05)
    assert turns.barges == [["a1"]]
    assert turns.finals == ["No wait, that is wrong."]
    assert ("backchannel_ignored", {"text": "Okay."}) in turns.call.rows
    # The browser's call state: agent audio, live partials (deduped), then thinking.
    assert turns.call.states == [
        ("agent_speaking", ""),
        ("rep_speaking", "mm okay"),
        ("rep_speaking", "No wait, that is wrong"),
        ("thinking", ""),
    ]


async def test_one_word_turn_while_the_agent_is_silent_is_a_turn() -> None:
    turns = FakeTurns()
    proc = OrchestratorProcessor(turns, HeardTracker())  # type: ignore[arg-type]
    await run_test(proc, frames_to_send=[TranscriptionFrame("Correct.", "rep", "t")])
    await asyncio.sleep(0.05)
    assert turns.finals == ["Correct."] and turns.barges == []


async def test_silence_fill_sends_zeros_through_input_gaps(monkeypatch: Any) -> None:
    fill = SilenceFill()
    sent: list[Any] = []

    async def capture(frame: Any, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        sent.append(frame)

    monkeypatch.setattr(fill, "push_frame", capture)
    fill._rate, fill._last = 16000, 0.0
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(fill._fill(), timeout=0.1)
    assert sent and all(f.audio == b"\x00\x00" * 320 for f in sent)
    assert processors.GAP_S < 0.1


# ---------------------------------------------------------------- budget


def test_voice_budget_refuses_new_sessions_at_the_cap(tmp_path: Path) -> None:
    day = datetime(2026, 10, 29, 12, tzinfo=UTC)
    budget = VoiceBudget(tmp_path / "b.db", limit_micros=10_000, clock=lambda: day)
    assert budget.admit()
    budget.add(9_999, stt_seconds=Decimal("60"), tts_chars=100)
    assert budget.admit()
    budget.add(1, stt_seconds=Decimal("1.5"), tts_chars=0)
    assert not budget.admit()
    next_day = VoiceBudget(
        tmp_path / "b.db", limit_micros=10_000, clock=lambda: datetime(2026, 10, 30, tzinfo=UTC)
    )
    assert next_day.admit()


# ---------------------------------------------------------------- socket bridge


class FakeVoice:
    def __init__(self) -> None:
        self.spoken: list[list[str]] = []
        self.dropped: list[list[str]] = []

    async def speak(self, utt: Utterance) -> None:
        self.spoken.append([sid for sid, _ in utt.sentences])

    async def speak_sentences(self, sentences: list[tuple[str, str]]) -> None:
        self.spoken.append([sid for sid, _ in sentences])

    async def drop(self, ids: list[str]) -> None:
        self.dropped.append(ids)

    async def stop(self) -> None:
        return None

    async def finish(self) -> None:
        return None


def _recv_until(ws: Any, kind: str) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = []
    while not got or got[-1].get("type") != kind:
        got.append(ws.receive_json())
    return got


def test_socket_bridge_speaks_replies_and_refuses_voice_over_budget(tmp_path: Path) -> None:
    settings = Settings(
        nlu_mode="oracle", nlg_mode="template", llm_profile="offline", db_path=":memory:",
        voice_stack="pipecat", deepgram_api_key="test-key", api_key_pool={},
        voice_daily_budget_usd=Decimal("0"),
    )  # fmt: skip
    audit = AuditLog(tmp_path / "ws.db")
    app = create_app(settings=settings, llm=FakeLLM(), audit=audit)
    with TestClient(app) as client, client.websocket_connect("/ws/call/vb") as ws:
        ws.send_json({"type": "start", "scenario": "fixtures/demo"})
        opening = [f["id"] for f in _recv_until(ws, "turn_done") if f["type"] == "say"]

        # Budget 0: the offer is refused, with the same message on the call's socket.
        r = client.post("/voice/offer?call_id=vb", json={"sdp": "", "type": "offer"})
        assert r.status_code == 429
        assert _recv_until(ws, "error")[-1]["message"] == r.json()["error"]

        conn = call_ws.live_call("vb")
        voice = FakeVoice()
        async def attach() -> None:
            conn.attach_voice(voice)  # the opening is still unheard: spoken on attach

        client.portal.call(attach)
        ws.send_json({"type": "sentence_done", "id": opening[0]})  # browser ack: ignored
        ws.send_json({"type": "text", "text": "Max six payments.", "oracle": {"stance": "info"}})
        reply = [f["id"] for f in _recv_until(ws, "turn_done") if f["type"] == "say"]
    assert voice.spoken[0] == opening
    assert voice.spoken[-1] == reply
    types = [e["type"] for e in audit.for_call("vb")]
    assert "voice_refused" in types
    assert not any(e["type"] == "sentence_done" for e in audit.for_call("vb"))
    audit.close()


# ---------------------------------------------------------------- call end (Phase 65)


async def test_finish_waits_for_the_last_line_to_play_then_ends(tmp_path: Path) -> None:
    call = FakeCall(None)
    settings = Settings(llm_profile="offline", db_path=":memory:", api_key_pool={})
    session = VoiceSession(
        "c1", call, settings=settings, budget=VoiceBudget(tmp_path / "b.db", limit_micros=1)
    )
    stopped: list[bool] = []

    async def stop() -> None:
        stopped.append(True)

    session.stop = stop  # type: ignore[method-assign]
    session.tracker.queued("s1")  # the handoff line, written to TTS, not played yet
    task = asyncio.create_task(session.finish(timeout_s=5, tail_s=0))
    await asyncio.sleep(0.2)
    assert not stopped and ("ended", "") not in call.states
    session.tracker.played("s1")
    await asyncio.wait_for(task, 1)
    assert stopped == [True] and call.states[-1] == ("ended", "")

    # Audio that never finishes: ends at the timeout.
    late = VoiceSession(
        "c2", call, settings=settings, budget=VoiceBudget(tmp_path / "b.db", limit_micros=1)
    )
    late.stop = stop  # type: ignore[method-assign]
    late.tracker.queued("s2")
    await asyncio.wait_for(late.finish(timeout_s=0.1, tail_s=0), 1)
    assert stopped == [True, True]


# ---------------------------------------------------------------- Phase 65 tuning


def _audio(ms: int, level: int = 3000) -> TTSAudioRawFrame:
    """``ms`` of a 220 Hz tone at ``level`` (0: silence), 16 kHz mono."""
    t = np.arange(16 * ms) / 16000
    tone = (level * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
    return TTSAudioRawFrame(tone.tobytes(), 16000, 1)


async def _gate(**kw: Any) -> tuple[processors.OutputGate, list[Any]]:
    gate, out = processors.OutputGate(**kw), []

    async def push(frame: Any, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        out.append(frame)

    gate.push_frame = push  # type: ignore[method-assign]
    gate._task = asyncio.create_task(gate._release())
    return gate, out


async def test_output_gate_paces_pauses_resumes_and_drops_on_interruption() -> None:
    gate, out = await _gate()
    gate.pause()
    for _ in range(5):
        await gate.process_frame(_audio(100), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.05)
    assert out == []  # paused: nothing reaches the output
    gate.resume()
    await asyncio.sleep(0.05)
    # Released only LEAD_S ahead of real time, not all at once.
    assert 1 <= len(out) <= 3
    await gate.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    assert not gate.holding and isinstance(out[-1], InterruptionFrame)
    gate._task.cancel()


async def test_output_gate_trims_leading_silence_of_a_reply_only() -> None:
    lines: list[tuple[str, dict[str, Any]]] = []
    gate, out = await _gate(log=lambda kind, **d: lines.append((kind, d)))
    await gate.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
    # Deepgram sends other frames between audio chunks: they wait behind the audio.
    await gate.process_frame(_audio(200, level=0), FrameDirection.DOWNSTREAM)
    await gate.process_frame(processors.ReplyStartFrame(), FrameDirection.DOWNSTREAM)
    silence_then_speech = _audio(50, level=0).audio + _audio(100).audio
    await gate.process_frame(
        TTSAudioRawFrame(silence_then_speech, 16000, 1), FrameDirection.DOWNSTREAM
    )
    await asyncio.sleep(0.02)
    assert isinstance(out[-1], processors.ReplyStartFrame)
    audio = [f for f in out if isinstance(f, TTSAudioRawFrame)]
    # The 250 ms lead-in is gone (onset found to within one 10 ms window).
    assert len(_audio(100).audio) <= len(audio[0].audio) <= len(_audio(110).audio)
    assert [d["ms"] for kind, d in lines if kind == "trim"] in ([240], [250])
    # A sentence queued behind playing audio keeps its pause.
    await gate.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(_audio(50, level=0), FrameDirection.DOWNSTREAM)
    assert len(gate._held[-1].audio) == len(_audio(50).audio)
    gate._task.cancel()


async def test_vad_pause_resumes_on_a_backchannel_and_stops_on_a_barge() -> None:
    turns, tracker = FakeTurns(), HeardTracker()
    tracker.queued("a1")
    proc = OrchestratorProcessor(turns, tracker)  # type: ignore[arg-type]
    proc.gate = processors.OutputGate()
    proc.filler_after_s = None
    sent: list[Any] = []

    async def push(frame: Any, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        sent.append(frame)

    proc.push_frame = push  # type: ignore[method-assign]
    await proc.on_rep_voice(True)  # "mm-hmm" starts over the agent
    assert proc.gate.paused
    await proc._final("Mm-hmm.")
    assert not proc.gate.paused and turns.barges == [] and sent == []
    await proc.on_rep_voice(False)
    await proc.on_rep_voice(True)  # a real interruption
    assert proc.gate.paused
    await proc._final("No, hold on, that is wrong.")
    await asyncio.sleep(0.01)
    assert not proc.gate.paused and turns.barges == [[]]
    assert any(isinstance(f, InterruptionFrame) for f in sent)
    await proc.aclose()


async def test_vad_pause_resumes_by_itself_when_no_words_come(monkeypatch: Any) -> None:
    monkeypatch.setattr(processors, "RESUME_AFTER_S", 0.01)
    tracker = HeardTracker()
    tracker.queued("a1")
    proc = OrchestratorProcessor(FakeTurns(), tracker)  # type: ignore[arg-type]
    proc.gate = processors.OutputGate()
    await proc.on_rep_voice(True)  # echo or a cough: no transcript follows
    await proc.on_rep_voice(False)
    await asyncio.sleep(0.05)
    assert not proc.gate.paused
    await proc.aclose()


async def test_filler_only_when_the_reply_is_late() -> None:
    proc = OrchestratorProcessor(FakeTurns(), HeardTracker())  # type: ignore[arg-type]
    proc.filler_after_s = 0.05
    sent: list[Any] = []

    async def push(frame: Any, direction: Any = FrameDirection.DOWNSTREAM) -> None:
        sent.append(frame)

    proc.push_frame = push  # type: ignore[method-assign]
    await proc._final("We need seventy percent.")
    await proc.speak([("s1", "Okay.")])  # on time: no filler
    await asyncio.sleep(0.1)
    assert [f.text for f in sent if isinstance(f, TTSSpeakFrame)] == ["Okay."]
    proc.tracker.played("s1")
    await proc._final("Seventy, final answer.")
    await asyncio.sleep(0.1)  # late
    assert [f.text for f in sent if isinstance(f, TTSSpeakFrame)][-1] == processors.FILLERS[0]
    await proc.aclose()


class MergingOrch:
    """The early turn is still in NLU when the final adds words."""

    def __init__(self) -> None:
        self.merged: list[str] = []
        self.release = asyncio.Event()
        self.result = Utterance.__new__(Utterance)

    async def on_creditor_text(self, text: str) -> Utterance:
        await self.release.wait()
        return self.result

    async def merge_into_nlu(self, text: str) -> Utterance | None:
        self.merged.append(text)
        self.release.set()
        return self.result

    async def withdraw_unheard_turn(self) -> bool:
        raise AssertionError("a merged turn is not withdrawn")


async def test_eager_turn_merges_the_words_the_rep_added() -> None:
    orch = MergingOrch()
    call = FakeCall(orch)
    turns = VoiceTurns(call, eager=True)  # type: ignore[arg-type]
    turns.on_eager("We can do eight payments")
    turns.on_resumed()
    await turns.on_final("We can do eight payments, at least a hundred dollars each.")
    assert orch.merged == ["at least a hundred dollars each."]
    assert call.emitted == [orch.result]
    assert ("eager_merged", {"suffix": "at least a hundred dollars each."}) in call.rows
