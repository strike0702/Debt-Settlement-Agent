"""Static contract checks for voice UI (REVIEW F06)."""

from __future__ import annotations

from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "app.js"


def test_tts_onerror_sends_sentence_done() -> None:
    """F06: utter.onerror must ack so server pending is not stuck."""
    src = APP_JS.read_text(encoding="utf-8")
    # Locate onerror handler body after utter.onend.
    idx = src.index("utter.onerror")
    chunk = src[idx : idx + 500]
    assert "sentence_done" in chunk
    assert "showNotice" in chunk
    assert "isBenignTtsError" in chunk


def test_tts_ignores_cancel_errors_without_scary_notice() -> None:
    """Barge/echo cancel must not toast 'Speech playback failed'."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "function isBenignTtsError" in src
    assert "interrupted" in src
    assert "BARGE_ECHO_GUARD_MS" in src
    assert "Listening…" in src
    assert "Transcribing…" in src


def test_tts_holds_utterance_reference() -> None:
    """Chrome GC of Utterance aborts playback; keep currentUtter alive."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "currentUtter = utter" in src
    assert "ttsGen" in src


def test_voice_barge_drops_contaminated_stt() -> None:
    """Voice barge stays on; contaminated TTS clips are dropped (no speakback)."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "utteranceContaminated" in src
    assert "browserIgnoreResults" in src
    assert "function maybeBargeFromMic" in src
    assert "function muteMicForTts" not in src
    assert "agentIsTalking" in src


def test_browser_stt_shows_listening_when_armed() -> None:
    """Listening must not wait for Chrome onspeechstart / VAD onSpeechStart."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "function maybeShowListening" in src
    assert "function scheduleListening" in src
    assert "function resetVadCapture" in src
    assert "function armMicAfterAgent" in src
    assert "Loading mic…" in src
    assert "maybeShowListening();" in src


def test_download_log_survives_call_end() -> None:
    """End chat disables compose controls; last started call stays exportable."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "let downloadCallId = null" in src
    assert "downloadCallId = callId" in src
    assert "const id = downloadCallId" in src
    assert "el.btnDownload.disabled = !downloadCallId" in src
    assert "el.btnDownload.disabled = !on" not in src


def test_browser_stt_pauses_during_tts() -> None:
    """Browser STT hearing TTS barges at '65%' and sticks on Listening."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "function pauseBrowserRecForTts" in src
    assert "function speakableText" in src
    assert "percent" in src
    assert "browserPausedForTts" in src


def test_rep_view_shows_human_terms_not_internals() -> None:
    """F10–F12: tiers as spoken text, en-US money, no intent/status chips for the rep."""
    src = APP_JS.read_text(encoding="utf-8")
    assert 'toLocaleString("en-US"' in src
    assert "toLocaleString(undefined" not in src
    assert "No special tiers" in src
    assert "from the ${ordinal(from)} payment" in src
    assert 'JSON.stringify(value) : "[]"' not in src
    assert "Last agent intent" not in src
    assert "termsTableHtml(terms, { showStatus: false })" in src
