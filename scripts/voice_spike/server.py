"""Phase 62 spike (throwaway): Pipecat + Deepgram over WebRTC, echoing fixed replies.

Pipeline: SmallWebRTCTransport in → DeepgramFluxSTTService → UserTurnProcessor
(Flux drives turn start/stop and interruptions; no local VAD) → EchoReply (a
fixed reply per finished rep turn, with money, percents and dates in it) →
DeepgramTTSService (Aura-2) → SmallWebRTCTransport out. Nothing here talks to
the app's orchestrator; Phase 63 builds the real pipeline.

Endpoints: ``GET /`` (client.html), ``POST /api/offer`` (non-trickle SDP
offer/answer), ``POST /say_opening`` (the agent speaks first), ``GET /events``
and ``POST /events/reset`` (pipeline events with epoch-ms timestamps, read by
``eval.voice.driver``), ``GET /healthz``. The Deepgram key is read server-side
from the repo's ``.env``; the page never sees it. Each session appends its Flux
seconds and Aura-2 characters to the harness's Deepgram usage ledger.

Run: ``uv run --project scripts/voice_spike python scripts/voice_spike/server.py --port 8762``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LEDGER = ROOT / "docs/eval/voice_spike_20261028/deepgram_usage.jsonl"
AGENT_VOICE = "aura-2-thalia-en"
EAGER_EOT_THRESHOLD = 0.5
FLUX_PER_MIN = Decimal("0.0077")
AURA2_PER_1K_CHARS = Decimal("0.030")

OPENING = (
    "Hi, this is the settlement agent from Synthetic Debt Relief, calling about an account "
    "with a balance of $1,250.00. We would like to talk about a settlement today."
)
# Fixed replies, in rotation: money, percents and dates in the formats
# Fact.render() produces today, so the run doubles as an Aura-2 reading check.
REPLIES = (
    "Thanks. So that is 8 monthly payments of $120.00 each, starting on November 15, 2026. "
    "Does that work for you?",
    "Understood. We could offer 42% of the balance, which is $525.00 in total, "
    "paid over 6 months.",
    "Got it. The first payment would be on Dec 1, 2026, and the last one on Jun 1, 2027, "
    "for a total of $562.50.",
    "Just to confirm, that is 45% of $1,250.00, so $562.50, in 5 payments of $112.50.",
)

EVENTS: list[dict] = []


def now_ms() -> float:
    return time.time() * 1000


def log(kind: str, **data: object) -> None:
    EVENTS.append({"t": now_ms(), "kind": kind, **data})


def deepgram_key() -> str:
    key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DEEPGRAM_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit("DEEPGRAM_API_KEY is not set")
    return key


def ledger(kind: str, *, chars: int = 0, seconds: float = 0.0, usd: Decimal, note: str) -> None:
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": kind,
        "chars": chars,
        "seconds": round(seconds, 2),
        "usd": str(usd.quantize(Decimal("0.000001"))),
        "note": note,
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(row) + "\n")


class Session:
    """The one live call: its task and what it cost."""

    task: PipelineTask | None = None
    started: float = 0.0
    tts_chars: int = 0
    replies: int = 0


SESSION = Session()


def speak(text: str) -> TTSSpeakFrame:
    SESSION.tts_chars += len(text)
    return TTSSpeakFrame(text)


class EchoReply(FrameProcessor):
    """On each final rep transcript, say the next fixed reply."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            log("final", text=frame.text)
            await self.push_frame(frame, direction)
            reply = REPLIES[SESSION.replies % len(REPLIES)]
            SESSION.replies += 1
            log("reply", text=reply)
            await self.push_frame(speak(reply))
            return
        if isinstance(frame, InterimTranscriptionFrame):
            log("interim", text=frame.text)
        elif isinstance(frame, InterruptionFrame):
            log("interruption")
        await self.push_frame(frame, direction)


class TtsProbe(FrameProcessor):
    """Logs the first TTS audio byte of each utterance (after the TTS service)."""

    def __init__(self) -> None:
        super().__init__()
        self._waiting = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            self._waiting = True
            log("tts_started")
        elif isinstance(frame, TTSAudioRawFrame) and self._waiting:
            self._waiting = False
            log("tts_byte")
        elif isinstance(frame, BotStartedSpeakingFrame):
            log("bot_started")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            log("bot_stopped")
        await self.push_frame(frame, direction)


def _flux_logger(kind: str) -> Callable[..., Awaitable[None]]:
    async def handler(_svc: object, transcript: str = "", *_rest: object) -> None:
        log(kind, text=transcript)

    return handler


async def run_bot(connection: object) -> None:
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    stt = DeepgramFluxSTTService(
        api_key=deepgram_key(),
        # Eager EOT only adds events here (logged, not acted on): it shows how much
        # earlier NLU could start. eot_threshold / eot_timeout_ms stay at defaults.
        settings=DeepgramFluxSTTService.Settings(
            model="flux-general-en", eager_eot_threshold=EAGER_EOT_THRESHOLD
        ),
    )
    for name in ("on_start_of_turn", "on_end_of_turn", "on_eager_end_of_turn"):
        stt.add_event_handler(name, _flux_logger(name.removeprefix("on_")))
    stt.add_event_handler("on_turn_resumed", _flux_logger("turn_resumed"))
    tts = DeepgramTTSService(
        api_key=deepgram_key(),
        settings=DeepgramTTSService.Settings(voice=AGENT_VOICE),
    )
    turns = UserTurnProcessor(user_turn_strategies=ExternalUserTurnStrategies())
    pipeline = Pipeline(
        [transport.input(), stt, turns, EchoReply(), tts, TtsProbe(), transport.output()]
    )
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))
    SESSION.task, SESSION.started, SESSION.tts_chars, SESSION.replies = task, time.time(), 0, 0

    @transport.event_handler("on_client_disconnected")
    async def _gone(_t: object, _c: object) -> None:
        await task.cancel()

    log("session_start")
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        secs = time.time() - SESSION.started
        log("session_end", seconds=secs)
        ledger("flux_stt", seconds=secs, usd=FLUX_PER_MIN * Decimal(str(secs)) / 60, note="spike")
        chars = SESSION.tts_chars
        ledger("aura2_tts", chars=chars, usd=AURA2_PER_1K_CHARS * chars / 1000, note="spike")
        SESSION.task = None


app = FastAPI()
handler = SmallWebRTCRequestHandler()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HERE / "client.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/offer")
async def offer(request: Request) -> dict | None:
    body = await request.json()

    async def on_connection(connection: object) -> None:
        asyncio.get_running_loop().create_task(run_bot(connection))

    return await handler.handle_web_request(SmallWebRTCRequest.from_dict(body), on_connection)


@app.post("/say_opening")
async def say_opening() -> dict:
    if SESSION.task is None:
        return {"ok": False}
    await SESSION.task.queue_frames([speak(OPENING)])
    return {"ok": True}


@app.get("/events")
async def events() -> list[dict]:
    return EVENTS


@app.post("/events/reset")
async def reset() -> dict:
    EVENTS.clear()
    return {"ok": True}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8762)
    args = ap.parse_args()
    if args.port in (8056, 8071):
        raise SystemExit("ports 8056 and 8071 are reserved for the user's review servers")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
