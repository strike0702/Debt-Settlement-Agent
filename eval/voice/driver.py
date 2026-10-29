"""Drive one scripted call through a voice stack in Chrome and collect its timeline.

Targets:

- ``spike``: the throwaway Pipecat + Deepgram page in ``scripts/voice_spike``
  (WebRTC audio both ways); its server is started from that folder's own
  environment, and its pipeline events come from ``GET /events``.
- ``pipecat``: the app itself (Phase 63; a local uvicorn this module starts
  and stops) and its minimal page ``/voice/test``: a real call (orchestrator, NLU, NLG) with Flux +
  Aura-2 over WebRTC; pipeline events come from ``GET /voice/events?call_id=``.
- ``pipecat-ui``: the same server, driven through the product UI's WebRTC
  client (Phase 64): Start call, then Turn voice on.

Audio sources (``--audio``): ``inject`` replaces ``getUserMedia`` with a WebAudio
stream the driver plays each rep WAV into, timed against the agent's speech
(no acoustic echo); ``speakers`` uses the real laptop mic and plays rep WAVs
with ``afplay``, so the agent's own voice reaches the mic (echo test).

Raw page events (``eval/voice/probe.js``) and server events are mapped to the
neutral kinds ``eval.voice.metrics`` scores. This module decides when each
rep line plays; it never decides what the agent says.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from eval.voice.cdp import Chrome, Page, open_page
from eval.voice.script import CallScript, RepLine

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures/voice"
PROBE_JS = (Path(__file__).parent / "probe.js").read_text()
SPIKE_DIR = ROOT / "scripts/voice_spike"

Target = Literal["spike", "pipecat", "pipecat-ui"]
Audio = Literal["inject", "speakers"]

REPLY_GAP_S = 0.6  # rep waits this long after the agent goes quiet


@dataclass
class Soak:
    """Long-call check (Phase 65): extra silence before each reply line, and a
    stall of the server process (SIGSTOP / SIGCONT) before one line, which the
    WebRTC client sees as a network blip it must recover from."""

    gap_s: float = 0.0
    blip_before: str | None = None
    blip_s: float = 0.0


SOAK = Soak()
SERVER_PIDS: list[int] = []
IDLE_S = 1.2  # agent quiet this long = its turn is over
REPLY_TIMEOUT_S = 25.0
SPEAK_TIMEOUT_S = 15.0


def now_ms() -> float:
    return time.time() * 1000


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def serve(
    cmd: list[str], *, port: int, cwd: Path, env: dict[str, str], log: Path
) -> AsyncIterator[str]:
    """Run a server process until ``/healthz`` answers; stop it on exit."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **env}, stdout=out, stderr=out)
        SERVER_PIDS.append(proc.pid)
        base = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient() as http:
                for _ in range(600):
                    if proc.poll() is not None:
                        raise RuntimeError(f"server exited early; see {log}")
                    with contextlib.suppress(httpx.HTTPError):
                        if (await http.get(f"{base}/healthz", timeout=1)).status_code == 200:
                            break
                    await asyncio.sleep(0.2)
                else:
                    raise RuntimeError(f"server did not come up; see {log}")
            yield base
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def app_server(port: int, log: Path, db: Path) -> contextlib.AbstractAsyncContextManager[str]:
    """The app on the demo profile, free NLU chain only (no paid Claude, no cache)."""
    env = {
        "LLM_PROFILE": "demo",
        "NLU_MODE": "llm",
        "NLG_MODE": "bank",
        # Budget 0 = the budgeted Claude NLU target counts as exhausted and is skipped.
        "CLAUDE_DAILY_BUDGET_USD": "0",
        "LLM_CACHE": "false",
        "DB_PATH": str(db),
    }
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    return serve(cmd, port=port, cwd=ROOT, env=env, log=log)


def spike_server(port: int, log: Path) -> contextlib.AbstractAsyncContextManager[str]:
    """The Pipecat spike in its own uv project (its deps never touch the app's)."""
    cmd = [
        "uv",
        "run",
        "--project",
        str(SPIKE_DIR),
        "python",
        str(SPIKE_DIR / "server.py"),
        "--port",
        str(port),
    ]
    return serve(cmd, port=port, cwd=SPIKE_DIR, env={}, log=log)


# ---------------------------------------------------------------- call run


@dataclass
class CallRun:
    """Everything one call produced: raw page events, server events, notes."""

    call: str
    target: str
    audio: str
    page: list[dict[str, Any]] = field(default_factory=list)
    server: list[dict[str, Any]] = field(default_factory=list)
    driver: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Session:
    """One Chrome page on one target; polls the probe's event log."""

    def __init__(self, page: Page, run: CallRun, audio: Audio) -> None:
        self.page = page
        self.run = run
        self.audio = audio
        self.cursor = 0

    async def poll(self) -> list[dict[str, Any]]:
        new = await self.page.eval(f"window.__vh ? __vh.since({self.cursor}) : []") or []
        self.cursor += len(new)
        self.run.page.extend(new)
        return new

    def mark(self, kind: str, **data: Any) -> None:
        self.run.driver.append({"t": now_ms(), "kind": kind, **data})

    async def play(self, line: RepLine) -> float:
        """Play a rep clip; returns its duration in ms (the call waits for it to end)."""
        path = FIXTURES / line.wav_name
        if self.audio == "inject":
            url = "data:audio/wav;base64," + base64.b64encode(path.read_bytes()).decode()
            dur = await self.page.eval(
                f"__vh.play({json.dumps(url)}, {json.dumps(line.id)})", gesture=True
            )
        else:
            import wave

            with wave.open(str(path)) as w:
                dur = w.getnframes() / w.getframerate() * 1000
            proc = await asyncio.create_subprocess_exec("afplay", str(path))
            t0 = now_ms()
            self.run.driver.append({"t": t0, "kind": "rep_start", "id": line.id, "dur_ms": dur})
            self.run.driver.append({"t": t0 + dur, "kind": "rep_end", "id": line.id})
            asyncio.get_running_loop().create_task(proc.wait())
        await asyncio.sleep(dur / 1000 + 0.05)
        return float(dur)

    async def speaking(self, target: Target) -> bool:
        return bool(await self.page.eval("__vhSpeaking()"))

    async def wait_until(self, cond: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.poll()
            if await cond():
                return True
            await asyncio.sleep(0.05)
        return False


SPIKE_HELPERS = """
window.__vhSpeaking = () => {
  const ev = window.__vh.events;
  for (let i = ev.length - 1; i >= 0; i--) {
    if (ev[i].kind === "agent_audio_on") return true;
    if (ev[i].kind === "agent_audio_off") return false;
  }
  return false;
};
"""


async def _wait_idle(s: Session, target: Target, *, since: float, timeout: float) -> bool:
    """Wait for the agent to speak after ``since`` and then go quiet for ``IDLE_S``."""
    started = await s.wait_until(lambda: _spoke_since(s, target, since), timeout)
    if not started:
        return False
    quiet_since: float | None = None

    async def quiet() -> bool:
        nonlocal quiet_since
        if await s.speaking(target):
            quiet_since = None
            return False
        quiet_since = quiet_since or time.monotonic()
        return time.monotonic() - quiet_since >= IDLE_S

    return await s.wait_until(quiet, 60)


async def _spoke_since(s: Session, target: Target, since: float) -> bool:
    return any(e["kind"] == "agent_audio_on" and e["t"] >= since for e in s.run.page)


async def _wait_speaking_for(s: Session, target: Target, secs: float, timeout: float) -> bool:
    """Wait until the agent has been speaking for ``secs`` without a gap."""
    stretch: float | None = None

    async def long_enough() -> bool:
        nonlocal stretch
        if not await s.speaking(target):
            stretch = None
            return False
        stretch = stretch or time.monotonic()
        return time.monotonic() - stretch >= secs

    return await s.wait_until(long_enough, timeout)


async def _stall(pid: int, secs: float) -> None:
    """Freeze the server (no packets, no answers) for ``secs``, then let it run."""
    import signal

    os.kill(pid, signal.SIGSTOP)
    try:
        await asyncio.sleep(secs)
    finally:
        os.kill(pid, signal.SIGCONT)


async def drive_call(s: Session, call: CallScript, target: Target) -> None:
    """Play every line of ``call`` at its moment."""
    _mark_rss(s)
    # The opening: the agent speaks first.
    since = now_ms() - 60_000
    for line in call.lines:
        if line.kind == "reply":
            if not await _wait_idle(s, target, since=since, timeout=REPLY_TIMEOUT_S):
                s.run.notes.append(f"{line.id}: agent did not finish a turn before this line")
            await asyncio.sleep(REPLY_GAP_S + SOAK.gap_s)
            if line.id == SOAK.blip_before and SERVER_PIDS:
                s.mark("blip", seconds=SOAK.blip_s)
                await _stall(SERVER_PIDS[-1], SOAK.blip_s)
                await asyncio.sleep(10)  # the client's reconnect, if any
                s.mark("blip_end")
            s.mark("line", id=line.id)
            await s.play(line)
            since = now_ms()
            continue
        # backchannel / interrupt: once the agent has talked for offset_ms without a break.
        if not await _wait_speaking_for(s, target, line.offset_ms / 1000, SPEAK_TIMEOUT_S):
            s.run.notes.append(
                f"{line.id}: agent never spoke {line.offset_ms} ms in a row; played anyway"
            )
        s.mark("line", id=line.id)
        await s.play(line)
        since = now_ms() if line.kind == "interrupt" else since
    await _wait_idle(s, target, since=since, timeout=REPLY_TIMEOUT_S)
    await asyncio.sleep(1.5)
    _mark_rss(s)
    await s.poll()


def _mark_rss(s: Session) -> None:
    """The server's resident memory (KB) on the timeline, for the long-call check."""
    if SERVER_PIDS:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(SERVER_PIDS[-1])], capture_output=True, text=True
        )
        if out.stdout.strip():
            s.mark("server_rss", kb=int(out.stdout.strip()))


async def run_spike(
    call: CallScript, *, base: str, audio: Audio, chrome_args: list[str]
) -> CallRun:
    run = CallRun(call.id, "spike", audio)
    cfg = {"inject": audio == "inject"}
    init = f"window.__VH_CFG = {json.dumps(cfg)};\n{PROBE_JS}\n{SPIKE_HELPERS}"
    async with (
        Chrome(extra_args=chrome_args, via_open=audio == "speakers") as chrome,
        httpx.AsyncClient() as http,
    ):
        page = await chrome.page()
        await http.post(f"{base}/events/reset")
        await open_page(page, f"{base}/", init_script=init)
        s = Session(page, run, audio)
        await page.eval("window.spikeConnect()", gesture=True)
        ok = await s.wait_until(
            lambda: _async_true(
                any(e["kind"] == "pc_state" and e["state"] == "connected" for e in run.page)
            ),
            20,
        )
        if not ok:
            raise RuntimeError("spike WebRTC did not connect")
        await http.post(f"{base}/say_opening")
        await drive_call(s, call, "spike")
        run.server = (await http.get(f"{base}/events")).json()
        await page.eval("window.spikeDisconnect()")
        await asyncio.sleep(0.5)
        await page.close()
    return run


async def run_pipecat(
    call: CallScript, *, base: str, audio: Audio, chrome_args: list[str]
) -> CallRun:
    run = CallRun(call.id, "pipecat", audio)
    cfg = {"inject": audio == "inject"}
    init = f"window.__VH_CFG = {json.dumps(cfg)};\n{PROBE_JS}\n{SPIKE_HELPERS}"
    call_id = f"vh-{call.id}-{int(now_ms())}"
    async with (
        Chrome(extra_args=chrome_args, via_open=audio == "speakers") as chrome,
        httpx.AsyncClient() as http,
    ):
        page = await chrome.page()
        url = f"{base}/voice/test?scenario_id={call.scenario_id}&call_id={call_id}"
        await open_page(page, url, init_script=init)
        s = Session(page, run, audio)
        await page.eval("window.voiceConnect()", gesture=True)
        ok = await s.wait_until(
            lambda: _async_true(
                any(e["kind"] == "pc_state" and e["state"] == "connected" for e in run.page)
            ),
            30,
        )
        if not ok:
            raise RuntimeError("pipecat WebRTC did not connect")
        await drive_call(s, call, "pipecat")
        await page.eval("window.voiceDisconnect()")
        await asyncio.sleep(1.5)
        run.server = (await http.get(f"{base}/voice/events", params={"call_id": call_id})).json()
        await page.close()
    return run


async def run_pipecat_ui(
    call: CallScript, *, base: str, audio: Audio, chrome_args: list[str]
) -> CallRun:
    """The product UI's WebRTC client (Phase 64): Start call, then Turn voice on."""
    run = CallRun(call.id, "pipecat-ui", audio)
    cfg = {"inject": audio == "inject"}
    init = f"window.__VH_CFG = {json.dumps(cfg)};\n{PROBE_JS}\n{SPIKE_HELPERS}"
    async with (
        Chrome(extra_args=chrome_args, via_open=audio == "speakers") as chrome,
        httpx.AsyncClient() as http,
    ):
        page = await chrome.page()
        await open_page(
            page, f"{base}/?scenario={call.scenario_id}&view=operator", init_script=init
        )
        s = Session(page, run, audio)
        started = False
        for _ in range(60):
            if await page.eval("__vh.click('Start call')", gesture=True):
                started = await s.wait_until(
                    lambda: _async_true(any(e["kind"] == "ws_in" for e in run.page)), 5
                )
                if started:
                    break
            await asyncio.sleep(0.25)
        if not started:
            raise RuntimeError("the call did not start")
        for _ in range(40):
            if await page.eval("__vh.click('Turn voice on')", gesture=True):
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("no voice button")
        ok = await s.wait_until(
            lambda: _async_true(
                any(e["kind"] == "pc_state" and e["state"] == "connected" for e in run.page)
            ),
            30,
        )
        if not ok:
            raise RuntimeError("pipecat WebRTC did not connect")
        # The page names its call only in the signaling URL.
        call_id = await page.eval(
            "decodeURIComponent((performance.getEntriesByType('resource').map((e) => e.name)"
            ".find((n) => n.includes('/voice/offer?call_id=')) || '').split('call_id=')[1] || '')"
        )
        await drive_call(s, call, "pipecat-ui")
        await page.eval("__vh.click('End call')", gesture=True)
        await asyncio.sleep(1.5)
        run.server = (await http.get(f"{base}/voice/events", params={"call_id": call_id})).json()
        await page.close()
    return run


async def _async_true(v: bool) -> bool:
    return v


# ---------------------------------------------------------------- normalize


def normalize(run: CallRun) -> list[dict[str, Any]]:
    """Map a run's raw events to the neutral kinds of ``eval.voice.metrics``."""
    out: list[dict[str, Any]] = []
    for e in run.page + run.driver:
        k = e["kind"]
        if k in ("rep_start", "rep_end"):
            out.append({"t": e["t"], "kind": k, "id": e["id"]})
    if run.target in ("pipecat", "pipecat-ui"):
        out += _normalize_pipecat(run)
    else:
        out += _normalize_spike(run)
    return sorted(out, key=lambda e: e["t"])


def _normalize_spike(run: CallRun) -> list[dict[str, Any]]:
    out = []
    for e in run.page:
        if e["kind"] == "agent_audio_on":
            out.append({"t": e["t"], "kind": "audio_on"})
        elif e["kind"] == "agent_audio_off":
            out.append({"t": e["t"], "kind": "audio_off"})
    # Pipecat broadcasts an interruption at every Flux start of turn, even when
    # the bot is silent; only one while the bot is speaking is a barge.
    speaking: list[tuple[float, float]] = []
    on: float | None = None
    for e in sorted(run.server, key=lambda x: x["t"]):
        if e["kind"] == "bot_started":
            on = e["t"]
        elif e["kind"] == "bot_stopped" and on is not None:
            speaking.append((on, e["t"]))
            on = None
    if on is not None:
        speaking.append((on, float("inf")))
    interrupted = [
        e["t"]
        for e in run.server
        if e["kind"] == "interruption" and any(lo <= e["t"] < hi for lo, hi in speaking)
    ]
    for e in run.server:
        k, t = e["kind"], e["t"]
        if k == "end_of_turn":
            out.append({"t": t, "kind": "eot"})
        elif k == "final":
            out.append({"t": t, "kind": "final", "text": e.get("text", "")})
        elif k == "reply":
            out.append({"t": t, "kind": "reply"})
        elif k == "tts_byte":
            out.append({"t": t, "kind": "tts_byte"})
        elif k == "interruption" and t in interrupted:
            out.append({"t": t, "kind": "barge"})
        elif k == "eager_end_of_turn":
            out.append({"t": t, "kind": "eager_eot"})
    # Agent audio that ends within 1.5 s of an interruption is the barge stop.
    for e in list(out):
        if e["kind"] == "audio_off" and any(0 <= e["t"] - t <= 1500 for t in interrupted):
            out.append({"t": e["t"], "kind": "stop"})
    return out


def _normalize_pipecat(run: CallRun) -> list[dict[str, Any]]:
    """Like the spike, but barges are the pipeline's own decisions (``barge`` events)."""
    out = []
    for e in run.page:
        if e["kind"] == "agent_audio_on":
            out.append({"t": e["t"], "kind": "audio_on"})
        elif e["kind"] == "agent_audio_off":
            out.append({"t": e["t"], "kind": "audio_off"})
    barges = [e["t"] for e in run.server if e["kind"] == "barge" and e.get("reason") != "dropped"]
    kinds = {
        "end_of_turn": "eot",
        "final": "final",
        "tts_byte": "tts_byte",
        "eager_end_of_turn": "eager_eot",
    }
    for e in run.server:
        k, t = e["kind"], e["t"]
        if k in kinds:
            out.append({"t": t, "kind": kinds[k], "text": e.get("text", "")})
        elif k == "reply":
            out.append({"t": t, "kind": "reply"})
        elif k == "barge" and t in barges:
            out.append({"t": t, "kind": "barge"})
    # Phase 65: a VAD pause stops the audio before the words make it a barge,
    # so a barge's stop may start at the pause that led to it.
    pauses = [e["t"] for e in run.server if e["kind"] == "pause"]
    led = [p for p in pauses if any(0 <= b - p <= 3000 for b in barges)]
    for p in pauses:
        out.append({"t": p, "kind": "pause"})
    # A filler's audio is not the reply's: relabel it, and take the reply's
    # first audio from the server when it follows the filler without a gap.
    fillers = [e["t"] for e in run.server if e["kind"] == "filler"]
    replies = [e["t"] for e in run.server if e["kind"] == "reply_audio"]
    on = [e["t"] for e in out if e["kind"] == "audio_on"]
    for e in out:
        if e["kind"] == "audio_on" and any(
            0 <= e["t"] - f <= 1500 and not any(f <= r <= e["t"] + 50 for r in replies)
            for f in fillers
        ):
            e["kind"] = "filler_on"
    for r in replies:
        if any(0 <= r - f <= 8000 for f in fillers) and not any(-50 <= t - r <= 400 for t in on):
            out.append({"t": r, "kind": "audio_on"})
    for e in list(out):
        if e["kind"] == "audio_off" and any(0 <= e["t"] - t <= 1500 for t in barges + led):
            out.append({"t": e["t"], "kind": "stop"})
    return out
