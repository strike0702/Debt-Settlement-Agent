"""Server voice pipeline (Phase 63): Pipecat + Deepgram over WebRTC, the only voice path.

Audio only: the browser sends its mic over WebRTC and plays the agent's Aura-2
audio; every UI event still goes over the call's WebSocket (``app.voice.ws``).
A voice session attaches to a call that already started on that socket, by
``call_id``, one session per call.

Modules: ``rules`` (barge and backchannel rule), ``heard`` (which sentences were
played), ``budget`` (daily Deepgram cap and audit sizes), ``processors`` (the
Pipecat frame processors), ``session`` (builds and runs one call's pipeline),
``routes`` (``/voice/offer`` and the harness test page). Policy and NLG stay in
``app.agent``; this package only moves text in and sentences out.
"""
