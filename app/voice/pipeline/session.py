"""One call's voice session: builds the Pipecat pipeline, runs it, audits and budgets it.

``VoiceSession.start`` is called by ``routes`` once WebRTC connects. It joins
the call (``VoiceCall``) that already runs on the WebSocket, at most one
session per call (``SESSIONS``). Each reply sentence sent to Aura-2 is audited
as ``tts_request`` (characters, sentence id, cost estimate) when it goes to
Aura-2 live, or as ``tts_cache_hit`` (sentence id only) when it plays from the
number-free sentence cache (``tts_cache``; Phase 70). When the session ends,
``stt_session`` records the Flux seconds, the live characters, the cache hits
and the session's estimated cost, which is added to ``VoiceBudget`` (live
characters only: a hit costs nothing). Deepgram is
called directly by Pipecat's services here, not through ``app.llm.client``;
NLU and NLG still go through it.

Hosted-demo cap: a session ends by itself, with a message on the call's socket,
once the call's voice seconds reach ``voice_session_max_s`` (``CALL_SECONDS``
counts all sessions of a call, so a reconnect does not reset it) or today's
Flux minutes run out (``VoiceBudget.remaining_seconds``).

When the call ends (handoff or close), ``finish`` lets the last sentences
play out, sends ``voice_state: ended`` and only then stops; the browser keeps
the link open until that event (the closing line used to be cut).

A session runs on a fresh WebRTC connection (full connect) or on one held
idle since Start call (``warm``; Phase 72). Either way it starts only when
voice turns on, so Deepgram and the caps see only voice-on time.

``events`` is a short timeline (epoch ms) for the voice harness
(``GET /voice/events``): Flux turn events, finals, replies, first TTS byte,
bot start/stop, barges, heard sentences, and ``connect`` (the browser's click
→ live ``connect_ms`` and which path it took). Agent and rep words only; no
private facts.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app.agent.orchestrator import Utterance
from app.agent.policy import Phase
from app.config import Settings
from app.llm.budget import micros_to_usd
from app.voice.pipeline.budget import (
    BUDGET_MESSAGE,
    CALL_CAP_MESSAGE,
    VoiceBudget,
    stt_cost_micros,
    tts_cost_micros,
)
from app.voice.pipeline.heard import HeardTracker
from app.voice.pipeline.processors import (
    OrchestratorProcessor,
    OutputGate,
    RepVad,
    SilenceFill,
    Tap,
)
from app.voice.pipeline.tts_cache import CachedDeepgramTTS, cache_dir, shared_cache
from app.voice.pipeline.turns import VoiceCall, VoiceTurns

FLUX_MODEL = "flux-general-en"
MAX_EVENTS = 4000
# After the call's last line: wait at most this long for its audio to finish,
# then this tail for the browser's jitter buffer, before ending voice.
FINISH_TIMEOUT_S = 20.0
FINISH_TAIL_S = 0.5

# call_id → its live voice session (one per call).
SESSIONS: dict[str, VoiceSession] = {}
# call_id → voice seconds its ended sessions used (the per-call cap); bounded.
CALL_SECONDS: OrderedDict[str, float] = OrderedDict()
MAX_CALLS_TRACKED = 500
# Timelines of recently ended sessions (harness reads them after hang-up).
LAST_EVENTS: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()


def now_ms() -> float:
    return time.time() * 1000


def vad_params(s: Settings) -> VADParams:
    """The pause-only VAD's thresholds, from settings (``voice_vad_*``)."""
    return VADParams(
        confidence=s.voice_vad_confidence,
        start_secs=s.voice_vad_start_s,
        min_volume=s.voice_vad_min_volume,
    )


class VoiceSession:
    """The pipeline of one call, from WebRTC connect to disconnect or call end."""

    def __init__(
        self,
        call_id: str,
        call: VoiceCall,
        *,
        settings: Settings,
        budget: VoiceBudget,
    ) -> None:
        self.call_id = call_id
        self.call = call
        self.settings = settings
        self.budget = budget
        self.events: list[dict[str, Any]] = []
        self.tracker = HeardTracker()
        self.turns = VoiceTurns(call, eager=settings.voice_eager_nlu)
        self.proc = OrchestratorProcessor(self.turns, self.tracker, log=self.log)
        self.proc.on_tts_request = self._audit_tts
        self.proc.on_tts_cache_hit = self._audit_hit
        self.proc.call_over = self._call_over
        self.task: PipelineTask | None = None
        self.started = 0.0
        self.connected = asyncio.Event()
        # The pipeline's StartFrame reached the sink: Flux and TTS are open (Phase 72).
        self.live = asyncio.Event()
        self._finishing = False
        self._cap_task: asyncio.Task[None] | None = None

    def log(self, kind: str, **data: Any) -> None:
        if len(self.events) < MAX_EVENTS:
            self.events.append({"t": now_ms(), "kind": kind, **data})

    def _audit_tts(self, sid: str, text: str) -> None:
        price = self.settings.deepgram_aura2_usd_per_1k_chars
        usd = micros_to_usd(tts_cost_micros(len(text), price))
        self.call.audit("tts_request", {"sentence_id": sid, "chars": len(text), "usd": str(usd)})

    def _audit_hit(self, sid: str | None) -> None:
        self.call.audit("tts_cache_hit", {"sentence_id": sid})

    def _call_over(self) -> bool:
        """The call has ended or is ending: no ack (the last line may already be out)."""
        orch = self.call.orch
        if self._finishing or orch is None:
            return True
        return orch.session.neg.phase in (Phase.END, Phase.ESCALATE)

    async def speak(self, utt: Utterance) -> None:
        """Send a reply's sentences to TTS (the call emitted its UI events already)."""
        await self.connected.wait()
        await self.proc.speak(list(utt.sentences))

    async def speak_sentences(self, sentences: list[tuple[str, str]]) -> None:
        """Speak sentences of a reply emitted before voice joined (the opening)."""
        await self.connected.wait()
        await self.proc.speak(sentences)

    async def drop(self, ids: list[str]) -> None:
        """The orchestrator dropped these sentence ids: stop their audio."""
        await self.proc.drop(ids)

    def _build(self, connection: Any) -> PipelineTask:
        s = self.settings
        key = s.deepgram_api_key.get_secret_value() if s.deepgram_api_key else ""
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        stt = DeepgramFluxSTTService(
            api_key=key,
            # Barges are ours (rules.is_barge): Pipecat would interrupt on every
            # Flux start of turn, backchannels and a silent agent included.
            should_interrupt=False,
            settings=DeepgramFluxSTTService.Settings(
                model=FLUX_MODEL,
                eager_eot_threshold=s.voice_eager_eot_threshold,
                eot_threshold=s.voice_eot_threshold,
                eot_timeout_ms=s.voice_eot_timeout_ms,
            ),
        )
        self._flux_events(stt)
        cache = None
        if s.voice_tts_cache_mb > 0:
            cache = shared_cache(cache_dir(s), s.voice_tts_cache_mb * 1024 * 1024)
        tts = CachedDeepgramTTS(
            cache=cache,
            on_synth=self.proc.synthesized,
            api_key=key,
            settings=CachedDeepgramTTS.Settings(voice=s.voice_tts_voice),
        )
        out_tap = Tap("out", log=self.log, on_played=self.proc.played)
        gate = OutputGate(log=self.log)
        hear: list[Any] = []
        self.proc.filler_after_s = (
            s.voice_filler_after_ms / 1000 if s.voice_filler_after_ms is not None else None
        )
        self.proc.ack_after_s = s.voice_ack_delay_ms / 1000 if s.voice_ack else None
        if s.voice_vad_pause:
            self.proc.gate = gate
            vad = SileroVADAnalyzer(params=vad_params(s))
            hear.append(RepVad(vad, self.proc.on_rep_voice))
        pipeline = Pipeline(
            [
                transport.input(),
                SilenceFill(),
                *hear,
                stt,
                self.proc,
                tts,
                Tap("tts", log=self.log),
                gate,
                transport.output(),
                out_tap,
            ]
        )
        task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))

        @task.event_handler("on_pipeline_started")
        async def _started(_t: object, _f: object) -> None:
            self.live.set()

        @transport.event_handler("on_client_connected")
        async def _connected(_t: object, _c: object) -> None:
            self.connected.set()

        @transport.event_handler("on_client_disconnected")
        async def _gone(_t: object, _c: object) -> None:
            await task.cancel()

        return task

    def _flux_events(self, stt: DeepgramFluxSTTService) -> None:
        async def start(_svc: object, transcript: str = "", *_: object) -> None:
            self.log("start_of_turn", text=transcript)
            self.proc.rep_resumed()

        async def end(_svc: object, transcript: str = "", *_: object) -> None:
            self.log("end_of_turn", text=transcript)

        async def eager(_svc: object, transcript: str = "", *_: object) -> None:
            self.log("eager_end_of_turn", text=transcript)
            if not self.proc.agent_talking:
                self.turns.on_eager(transcript)

        async def resumed(_svc: object, *_: object) -> None:
            self.log("turn_resumed")
            self.proc.rep_resumed()
            self.turns.on_resumed()

        stt.add_event_handler("on_start_of_turn", start)
        stt.add_event_handler("on_end_of_turn", end)
        stt.add_event_handler("on_eager_end_of_turn", eager)
        stt.add_event_handler("on_turn_resumed", resumed)

    async def run(self, connection: Any) -> None:
        """Run until the browser disconnects or ``stop``; then audit and record the cost."""
        self.task = self._build(connection)
        self.started = time.monotonic()
        self.log("session_start")
        self.call.audit("voice_session_start", {"stack": "pipecat"})
        self._cap_task = asyncio.get_running_loop().create_task(self._cap())
        try:
            await PipelineRunner(handle_sigint=False).run(self.task)
        finally:
            self._cap_task.cancel()
            await self.turns.aclose()
            await self.proc.aclose()
            self._close()

    def cap_seconds(self) -> tuple[float, str]:
        """Seconds this session may run, and the message shown when they run out."""
        left = self.settings.voice_session_max_s - CALL_SECONDS.get(self.call_id, 0.0)
        daily = self.budget.remaining_seconds()
        if daily is not None and float(daily) < left:
            return max(0.0, float(daily)), BUDGET_MESSAGE
        return max(0.0, left), CALL_CAP_MESSAGE

    async def _cap(self) -> None:
        seconds, message = self.cap_seconds()
        await asyncio.sleep(seconds)
        self.log("capped", seconds=seconds)
        self.call.audit("voice_capped", {"seconds": seconds, "reason": message})
        await self.call.voice_error(message)
        await self.finish()

    def _close(self) -> None:
        s = self.settings
        secs = Decimal(str(round(time.monotonic() - self.started, 2)))
        stt = stt_cost_micros(secs, s.deepgram_flux_usd_per_min)
        tts = tts_cost_micros(self.proc.tts_chars, s.deepgram_aura2_usd_per_1k_chars)
        self.budget.add(stt + tts, stt_seconds=secs, tts_chars=self.proc.tts_chars)
        self.call.audit(
            "stt_session",
            {
                "seconds": str(secs),
                "stt_usd": str(micros_to_usd(stt)),
                "tts_chars": self.proc.tts_chars,
                "tts_cache_hits": self.proc.tts_cache_hits,
                "tts_usd": str(micros_to_usd(tts)),
                "usd": str(micros_to_usd(stt + tts)),
            },
        )
        self.log("session_end", seconds=float(secs))
        CALL_SECONDS[self.call_id] = CALL_SECONDS.pop(self.call_id, 0.0) + float(secs)
        while len(CALL_SECONDS) > MAX_CALLS_TRACKED:
            CALL_SECONDS.popitem(last=False)
        LAST_EVENTS[self.call_id] = self.events
        while len(LAST_EVENTS) > 20:
            LAST_EVENTS.popitem(last=False)
        if SESSIONS.get(self.call_id) is self:
            del SESSIONS[self.call_id]

    async def finish(
        self, *, timeout_s: float = FINISH_TIMEOUT_S, tail_s: float = FINISH_TAIL_S
    ) -> None:
        """The call is over: let its last sentences play, send ``ended``, then stop.

        Stopping at once cut the closing line before Aura-2 played it (live
        test 2026-10-29); the browser now keeps the link until ``ended``.
        """
        if self._finishing:
            return
        self._finishing = True
        deadline = time.monotonic() + timeout_s
        while self.proc.agent_talking and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        timed_out = self.proc.agent_talking
        await asyncio.sleep(tail_s)
        self.log("finish", timed_out=timed_out)
        await self.call.voice_state("ended", "")
        await self.stop()

    async def stop(self) -> None:
        """End the session (the call's socket closed)."""
        if self.task is not None:
            await self.task.cancel()
