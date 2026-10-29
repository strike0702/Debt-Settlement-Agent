"""Minimal Chrome DevTools Protocol client for the voice harness.

Launches a headed Chrome with a throwaway profile and talks CDP over the
page's WebSocket: evaluate JS, add an init script, navigate. It is just
enough to drive the product UI and the voice test pages from Python
without adding Playwright. It does not know anything about calls or audio;
``eval.voice.driver`` owns that.

Chrome is the user's installed Google Chrome. Every launch uses its own
``--user-data-dir``, so the user's own Chrome session is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection, connect

CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class CdpError(RuntimeError):
    """A CDP command returned an error or a page script threw."""


class Page:
    """One page target: send commands, evaluate expressions."""

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self._ws:
            msg = json.loads(raw)
            fut = self._pending.pop(msg.get("id", -1), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one CDP command and return its ``result``."""
        cid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        await self._ws.send(json.dumps({"id": cid, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(fut, timeout=60)
        if "error" in msg:
            raise CdpError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def eval(self, expr: str, *, gesture: bool = False) -> Any:
        """Evaluate ``expr`` (awaiting a promise) and return its JSON value.

        ``gesture=True`` marks the call as a user gesture, which Chrome needs
        before it lets a page start audio or ``speechSynthesis``.
        """
        res = await self.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": gesture,
            },
        )
        if "exceptionDetails" in res:
            detail = res["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise CdpError(f"page script failed: {text}")
        return res.get("result", {}).get("value")

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(Exception):
            await self._ws.close()


class Chrome:
    """A Chrome process with its own profile; ``async with`` stops it."""

    def __init__(
        self,
        *,
        extra_args: list[str] | None = None,
        binary: str = CHROME_MAC,
        via_open: bool = False,
    ) -> None:
        """``via_open`` launches through macOS ``open``, so Chrome is its own
        "responsible process" and the real mic uses Chrome's own mic permission
        (a child of the terminal would need the terminal's, which may be unset).
        """
        self.binary = binary
        self.via_open = via_open
        self.extra_args = extra_args or []
        self.pid: int | None = None
        self.profile = Path(tempfile.mkdtemp(prefix="dsa-voice-chrome-"))
        self.proc: subprocess.Popen[bytes] | None = None
        self.port: int | None = None

    async def __aenter__(self) -> Chrome:
        args = [
            f"--user-data-dir={self.profile}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            # Auto-accept the mic prompt; the device itself is chosen by the driver.
            "--use-fake-ui-for-media-stream",
            "--window-size=1280,900",
            *self.extra_args,
            "about:blank",
        ]
        if self.via_open:
            app = self.binary.split(".app/")[0] + ".app"
            subprocess.run(["open", "-na", app, "--args", *args], check=True)
        else:
            self.proc = subprocess.Popen(
                [self.binary, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        port_file = self.profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.exists() or not port_file.read_text().strip():
            if time.monotonic() > deadline:
                raise CdpError("Chrome did not open its DevTools port")
            await asyncio.sleep(0.1)
        self.port = int(port_file.read_text().splitlines()[0])
        if self.via_open:
            self.pid = _main_pid(self.profile)
        return self

    async def page(self) -> Page:
        """Attach to the first page target."""
        async with httpx.AsyncClient() as http:
            targets = (await http.get(f"http://127.0.0.1:{self.port}/json")).json()
        url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        ws = await connect(url, max_size=64 * 1024 * 1024)
        page = Page(ws)
        await page.send("Page.enable")
        await page.send("Runtime.enable")
        return page

    async def __aexit__(self, *exc: object) -> None:
        if self.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGTERM)
            await asyncio.sleep(1.0)
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def _main_pid(profile: Path) -> int | None:
    """PID of the browser process (not a ``--type=`` helper) using ``profile``."""
    out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if f"--user-data-dir={profile}" in args and "--type=" not in args:
            return int(pid)
    return None


async def open_page(page: Page, url: str, *, init_script: str | None = None) -> None:
    """Navigate ``page`` to ``url``, with ``init_script`` run before page scripts."""
    if init_script:
        await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": init_script})
    await page.send("Page.navigate", {"url": url})
    for _ in range(200):
        if await page.eval("document.readyState") == "complete":
            return
        await asyncio.sleep(0.05)
    raise CdpError(f"page did not load: {url}")
