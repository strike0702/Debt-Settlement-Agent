"""One call's voice session: builds the Pipecat pipeline, runs it, audits and budgets it.

``VoiceSession.start`` is called by ``routes`` once WebRTC connects. It joins
the call (``VoiceCall``) that already runs on the WebSocket, at most one
session per call (``SESSIONS``). Each reply sentence sent to Aura-2 is audited
as ``tts_request`` (characters, sentence id, cost estimate); when the session
ends, ``stt_session`` records the Flux seconds, the characters and the
session's estimated cost, which is added to ``VoiceBudget``. Deepgram is
called directly by Pipecat's services here, not through ``app.llm.client``;
NLU and NLG still go through it.

``events`` is a short timeline (epoch ms) for the voice harness
(``GET /voice/events``): Flux turn events, finals, replies, first TTS byte,
bot start/stop, barges and heard sentences. Agent and rep words only; no
private facts.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app.agent.orchestrator import Utterance
from app.config import Settings
from app.llm.budget import micros_to_usd
from app.voice.pipeline.budget import VoiceBudget, stt_cost_micros, tts_cost_micros
from app.voice.pipeline.heard import HeardTracker
from app.voice.pipeline.processors import OrchestratorProcessor, SilenceFill, Tap
from app.voice.pipeline.turns import VoiceCall, VoiceTurns

FLUX_MODEL = "flux-general-en"
MAX_EVENTS = 4000

# call_id → its live voice session (one per call).
SESSIONS: dict[str, VoiceSession] = {}
# Timelines of recently ended sessions (harness reads them after hang-up).
LAST_EVENTS: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()


def now_ms() -> float:
    return time.time() * 1000


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
        self.task: PipelineTask | None = None
        self.started = 0.0
        self.connected = asyncio.Event()

    def log(self, kind: str, **data: Any) -> None:
        if len(self.events) < MAX_EVENTS:
            self.events.append({"t": now_ms(), "kind": kind, **data})

    def _audit_tts(self, sid: str, text: str) -> None:
        price = self.settings.deepgram_aura2_usd_per_1k_chars
        usd = micros_to_usd(tts_cost_micros(len(text), price))
        self.call.audit("tts_request", {"sentence_id": sid, "chars": len(text), "usd": str(usd)})

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
        tts = DeepgramTTSService(
            api_key=key, settings=DeepgramTTSService.Settings(voice=s.voice_tts_voice)
        )
        out_tap = Tap("out", log=self.log, on_played=self.proc.played)
        pipeline = Pipeline(
            [
                transport.input(),
                SilenceFill(),
                stt,
                self.proc,
                tts,
                Tap("tts", log=self.log),
                transport.output(),
                out_tap,
            ]
        )
        task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))

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

        async def end(_svc: object, transcript: str = "", *_: object) -> None:
            self.log("end_of_turn", text=transcript)

        async def eager(_svc: object, transcript: str = "", *_: object) -> None:
            self.log("eager_end_of_turn", text=transcript)
            if not self.proc.agent_talking:
                self.turns.on_eager(transcript)

        async def resumed(_svc: object, *_: object) -> None:
            self.log("turn_resumed")
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
        try:
            await PipelineRunner(handle_sigint=False).run(self.task)
        finally:
            await self.turns.aclose()
            await self.proc.aclose()
            self._close()

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
                "tts_usd": str(micros_to_usd(tts)),
                "usd": str(micros_to_usd(stt + tts)),
            },
        )
        self.log("session_end", seconds=float(secs))
        LAST_EVENTS[self.call_id] = self.events
        while len(LAST_EVENTS) > 20:
            LAST_EVENTS.popitem(last=False)
        if SESSIONS.get(self.call_id) is self:
            del SESSIONS[self.call_id]

    async def stop(self) -> None:
        """End the session (the call's socket closed)."""
        if self.task is not None:
            await self.task.cancel()
