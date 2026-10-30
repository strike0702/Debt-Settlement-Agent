"""ICE servers for the voice path: Cloudflare STUN plus short-lived Cloudflare TURN.

``ice_servers(settings)`` asks Cloudflare's ``generate-ice-servers`` endpoint
for credentials that expire after ``voice_turn_ttl_s`` (about an hour) and
returns the ``iceServers`` list in the browser's ``RTCIceServer`` shape. It is
called per voice connection by ``routes`` (``GET /voice/ice`` for the browser,
and before each new offer for the Pipecat / aiortc side); nothing is cached,
so no credential outlives its ttl here. The long-term API token is only sent
to Cloudflare: never returned, never logged, never put in a URL.

Without ``CLOUDFLARE_TURN_KEY_ID`` / ``CLOUDFLARE_TURN_API_TOKEN`` (or when
Cloudflare fails) the list is STUN only, with one warning in the log. That
works on a laptop but not behind Render's HTTP-only ingress.

``server_ice_servers(settings)`` is the aiortc side's list: the same, or STUN
only when ``voice_server_turn`` is off (a host with a public IP needs no relay
of its own; the browser's TURN candidate still reaches it).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

_log = logging.getLogger(__name__)

STUN_ONLY: list[dict[str, Any]] = [{"urls": ["stun:stun.cloudflare.com:3478"]}]
GENERATE_URL = (
    "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
)
TIMEOUT_S = 5.0
_warned = False


def _warn_once(message: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        _log.warning(message)


def _usable(url: str) -> bool:
    # Browsers block port 53, and the client does not trickle ICE: a :53 URL
    # would only make candidate gathering wait for its timeout (Cloudflare docs).
    return ":53?" not in url and not url.endswith(":53")


def _clean(servers: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for server in servers:
        urls = server.get("urls")
        urls = [urls] if isinstance(urls, str) else list(urls or [])
        kept = [u for u in urls if isinstance(u, str) and _usable(u)]
        if kept:
            out.append({**server, "urls": kept})
    return out


async def ice_servers(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """Fresh ICE servers (STUN + TURN with short-lived credentials), or STUN only."""
    key_id = settings.cloudflare_turn_key_id
    token = settings.cloudflare_turn_api_token
    if not key_id or token is None:
        _warn_once("voice: no Cloudflare TURN key set; STUN only (hosted voice will not connect)")
        return [dict(s) for s in STUN_ONLY]
    own = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        res = await http.post(
            GENERATE_URL.format(key_id=key_id),
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            json={"ttl": settings.voice_turn_ttl_s},
        )
        res.raise_for_status()
        servers = _clean(res.json()["iceServers"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError) as e:
        # Status or exception type only: the request carries the token.
        status = isinstance(e, httpx.HTTPStatusError)
        detail = e.response.status_code if status else type(e).__name__
        _log.warning("voice: Cloudflare TURN credentials failed (%s); STUN only", detail)
        return [dict(s) for s in STUN_ONLY]
    finally:
        if own:
            await http.aclose()
    return servers or [dict(s) for s in STUN_ONLY]


def to_pipecat(servers: list[dict[str, Any]]) -> list[Any]:
    """The same servers as aiortc ``RTCIceServer`` objects (Pipecat's ``IceServer``)."""
    from pipecat.transports.smallwebrtc.connection import IceServer

    return [
        IceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
        for s in servers
    ]


async def server_ice_servers(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """ICE servers for the Pipecat / aiortc side; STUN only when ``voice_server_turn`` is off."""
    if not settings.voice_server_turn:
        return STUN_ONLY
    return await ice_servers(settings, client=client)
