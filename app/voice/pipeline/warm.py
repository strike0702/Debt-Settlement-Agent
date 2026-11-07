"""Idle pre-connections (Phase 72): a call's WebRTC link held open before voice is on.

On Start call the browser opens its peer connection with no mic track
(``POST /voice/offer?call_id=…&warm=1``). ``routes`` keeps the resulting Pipecat
``SmallWebRTCConnection`` here instead of building a ``VoiceSession``, so no
pipeline runs, no Deepgram socket is open and nothing counts against the voice
budget or the per-call voice cap. Turn voice on (``POST /voice/live``) takes it
back with ``take`` and starts the session on it; Pipecat fires the transport's
``connected`` event at once for a link that is already up.

A held link is closed when its call ends, after ``voice_preconnect_idle_s``,
when the browser drops it (tab hidden, ``DELETE /voice/warm``) or when a newer
one replaces it. ``take`` refuses one older than the TURN credentials' ttl
(``voice_turn_ttl_s``): the browser then does the full connect with fresh ones.
This module holds connections only; it never builds a pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

# How often a held link checks that its call is still live.
CHECK_S = 5.0


@dataclass
class Warm:
    """One call's held connection and the task that closes it when idle."""

    call_id: str
    connection: Any
    created: float
    reaper: asyncio.Task[None] | None = field(default=None, repr=False)


# call_id → its held pre-connection (at most one per call).
WARM: dict[str, Warm] = {}


async def discard(connection: Any) -> None:
    """Close a connection no longer held (best effort)."""
    try:
        await connection.disconnect()
    except Exception as e:  # a half-closed aiortc peer; nothing to keep
        _log.debug("voice: closing a pre-connection failed (%s)", type(e).__name__)


def _usable(connection: Any) -> bool:
    return connection.pc.connectionState not in ("closed", "failed")


async def hold(
    call_id: str,
    connection: Any,
    *,
    idle_s: float,
    call_live: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
) -> Warm:
    """Keep ``connection`` for ``call_id`` (closing an older one) until taken or idle."""
    await close(call_id)
    warm = Warm(call_id, connection, clock())

    async def reap() -> None:
        deadline = warm.created + idle_s
        while clock() < deadline and call_live() and _usable(connection):
            await asyncio.sleep(min(CHECK_S, max(0.0, deadline - clock())))
        if WARM.get(call_id) is warm:
            del WARM[call_id]
        await discard(connection)

    WARM[call_id] = warm
    warm.reaper = asyncio.get_running_loop().create_task(reap())
    return warm


async def take(
    call_id: str,
    pc_id: str | None,
    *,
    ttl_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> Any | None:
    """The held connection for ``call_id``/``pc_id``, now owned by the caller; else None.

    None when there is none, the pc id differs, the link closed, or it is older
    than ``ttl_s`` (closed here; the browser reconnects with fresh credentials).
    """
    warm = WARM.get(call_id)
    if warm is None or warm.connection.pc_id != pc_id:
        return None
    del WARM[call_id]
    if warm.reaper is not None:
        warm.reaper.cancel()
    if clock() - warm.created >= ttl_s or not _usable(warm.connection):
        await discard(warm.connection)
        return None
    return warm.connection


async def close(call_id: str, pc_id: str | None = None) -> None:
    """Drop the call's held connection, if any (only that peer's when ``pc_id`` is given)."""
    warm = WARM.get(call_id)
    if warm is None or (pc_id is not None and warm.connection.pc_id != pc_id):
        return
    del WARM[call_id]
    if warm.reaper is not None:
        warm.reaper.cancel()
    await discard(warm.connection)


async def close_all() -> None:
    """Drop every held connection (app shutdown)."""
    for call_id in list(WARM):
        await close(call_id)
