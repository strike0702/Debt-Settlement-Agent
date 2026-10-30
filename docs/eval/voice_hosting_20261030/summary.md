# Voice hosting check (Phase 66, 2026-10-30)

One local call through the product UI with the TURN relay forced
(`uv run python -m eval.voice --target pipecat-ui --ice relay --calls numbers_easy`;
the page opens with `?ice=relay`, so Chrome uses `iceTransportPolicy: "relay"`).
Injected audio, free Groq NLU. Not a hosted run: the user deploys to Render and
checks from another network (see PROGRESS, Phase 66).

| Measure | Value |
| --- | --- |
| Selected candidate pair | local `relay` (UDP, Cloudflare TURN) ↔ remote `srflx` (aiortc via Cloudflare STUN) |
| Pair RTT (getStats) | 128 ms |
| Mic granted → peer connected | 2548 ms (includes `GET /voice/ice` and ICE gathering) |
| Replies | 6/6; first reply audio p50 3748 / p95 4544 ms |
| Numbers | 5/5; first word lost 0; false barges 0; interruption 1/1 (stop 1314 ms) |
| Server peak RSS (sampled every 0.5 s) | 190 MB (194,592 KB) |
| Call span | 111 s |
| Deepgram (estimate) | Flux 1.9 min $0.014 + Aura-2 about 900 chars $0.027 = about $0.04 |

Startup, production command (`uvicorn app.main:app`, Pipecat imported at
startup): first `/healthz` 4.5 s on this laptop (warm disk), idle RSS 192 MB;
`import app.main` 1.9 CPU-seconds, of which Pipecat (via
`app.voice.pipeline.routes`) is about 0.48 s and `anthropic` 0.69 s.

Harness output: `relay/` (`summary.md` is the standard harness table).
