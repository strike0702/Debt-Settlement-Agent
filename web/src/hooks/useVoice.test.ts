/**
 * Voice behaviour contracts, ported from tests/unit/test_app_js_contracts.py
 * (which grepped the old app.js) to behaviour tests on `useVoice` with fake
 * browser deps: TTS acks (F06), benign cancel errors, utterance retention and
 * stale handlers, barge-in with spoken ids, the echo guard, contaminated clips,
 * listening cues, browser STT pausing during TTS, Phase 21 VAD settings, the
 * onstart timing stamp, the local backchannel, and preferred voices.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { useVoice } from "@/hooks/useVoice";
import {
  BACKCHANNEL_AFTER_MS,
  BACKCHANNEL_TEXT,
  BARGE_ECHO_GUARD_MS,
  BROWSER_STT_SETTLE_MS,
  LABEL,
  type RecLike,
  type UtterLike,
  type VoiceDeps,
  type VoiceIO,
} from "@/lib/voice/engine";
import { isBenignTtsError, pickVoice, PREFERRED_VOICES, speakableText } from "@/lib/voice/speech";
import { ONNX_RUNTIME_VERSION, VAD_OPTIONS, VAD_WEB_VERSION, type VadCallbacks } from "@/lib/voice/vad";
import { encodeWav } from "@/lib/voice/wav";
import type { ClientEvent, ServerEvent } from "@/types/protocol";

class FakeUtter implements UtterLike {
  rate = 1;
  voice: UtterLike["voice"] = null;
  lang = "";
  onstart: UtterLike["onstart"] = null;
  onend: UtterLike["onend"] = null;
  onerror: UtterLike["onerror"] = null;
  constructor(public text: string) {}
}

class FakeRec implements RecLike {
  static all: FakeRec[] = [];
  continuous = false;
  interimResults = false;
  onstart: RecLike["onstart"] = null;
  onspeechstart: RecLike["onspeechstart"] = null;
  onresult: RecLike["onresult"] = null;
  onerror: RecLike["onerror"] = null;
  onend: RecLike["onend"] = null;
  started = 0;
  aborted = 0;
  constructor() {
    FakeRec.all.push(this);
  }
  start() {
    this.started += 1;
  }
  stop() {}
  abort() {
    this.aborted += 1;
  }
  final(text: string) {
    this.onresult?.({ resultIndex: 0, results: { length: 1, 0: { isFinal: true, 0: { transcript: text } } } });
  }
}

interface Harness {
  sent: ClientEvent[];
  wavs: ArrayBuffer[];
  repTexts: string[];
  onsets: [number, number][];
  spoken: FakeUtter[];
  vad: { cb: VadCallbacks | null; start: Mock<() => void>; pause: Mock<() => void> };
  voices: { name: string; lang: string }[];
  deps: VoiceDeps;
  io: VoiceIO;
}

function harness(opts: { synth?: boolean; vadGate?: Promise<void> } = {}): Harness {
  const h = {
    sent: [] as ClientEvent[],
    wavs: [] as ArrayBuffer[],
    repTexts: [] as string[],
    onsets: [] as [number, number][],
    spoken: [] as FakeUtter[],
    vad: { cb: null as VadCallbacks | null, start: vi.fn<() => void>(), pause: vi.fn<() => void>() },
    voices: [] as { name: string; lang: string }[],
  } as Harness;
  const synth = {
    speak: (u: UtterLike) => h.spoken.push(u as FakeUtter),
    cancel: vi.fn(),
    resume: vi.fn(),
    getVoices: () => h.voices,
  };
  h.deps = {
    now: () => Date.now(),
    synth: opts.synth === false ? null : synth,
    makeUtterance: (t) => new FakeUtter(t),
    getUserMedia: async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream,
    createVad: async (_stream, cb) => {
      await opts.vadGate;
      h.vad.cb = cb;
      return { start: h.vad.start, pause: h.vad.pause, destroy: vi.fn<() => void>() };
    },
    SpeechRecognition: FakeRec,
  };
  h.io = {
    sendJson: (ev) => h.sent.push(ev),
    sendWav: (w) => (h.wavs.push(w), true),
    sendRepText: (t) => h.repTexts.push(t),
    currentTurn: () => 3,
    onTtsOnset: (turn, ms) => h.onsets.push([turn, ms]),
  };
  return h;
}

function mount(h: Harness, mode: "server" | "browser" = "server") {
  return renderHook(() => useVoice(h.io, h.deps, mode));
}

type Hook = ReturnType<typeof mount>;

const server = (hook: Hook, ev: ServerEvent) => act(() => hook.result.current.engine.onServerEvent(ev));
/** Deliver a `say` and let the engine's 20 ms speak() tick run. */
function say(hook: Hook, h: Harness, id: string, text = "Hello there.") {
  server(hook, { type: "say", id, text });
  act(() => vi.advanceTimersByTime(20));
  return h.spoken.at(-1)!;
}
const types = (h: Harness) => h.sent.map((e) => e.type);
const acks = (h: Harness) => h.sent.filter((e) => e.type === "sentence_done").map((e) => (e as { id: string }).id);
async function micOn(hook: Hook) {
  await act(async () => {
    await hook.result.current.engine.startMic();
  });
}
const audio = new Float32Array(1600);

beforeEach(() => {
  vi.useFakeTimers();
  FakeRec.all = [];
});
afterEach(() => vi.useRealTimers());

describe("TTS acks (F06)", () => {
  it("acks with sentence_done and shows a notice when playback fails", () => {
    const h = harness();
    const hook = mount(h);
    const u = say(hook, h, "s1");
    act(() => u.onerror?.({ error: "synthesis-failed" }));
    expect(acks(h)).toEqual(["s1"]);
    expect(hook.result.current.snapshot.notice).toMatch(/Speech playback failed/);
  });

  it("acks a benign cancel error without the scary notice", () => {
    const h = harness();
    const hook = mount(h);
    const u = say(hook, h, "s1");
    act(() => u.onerror?.({ error: "interrupted" }));
    expect(acks(h)).toEqual(["s1"]);
    expect(hook.result.current.snapshot.notice).toBe("");
    expect(["interrupted", "canceled", "cancelled"].every(isBenignTtsError)).toBe(true);
    expect(isBenignTtsError("network")).toBe(false);
  });

  it("acks every sentence in order when spoken, and still acks with no speechSynthesis", () => {
    const h = harness();
    const hook = mount(h);
    server(hook, { type: "say", id: "a", text: "One." });
    server(hook, { type: "say", id: "b", text: "Two." });
    act(() => vi.advanceTimersByTime(20));
    act(() => h.spoken.at(-1)!.onend?.());
    act(() => vi.advanceTimersByTime(20));
    act(() => h.spoken.at(-1)!.onend?.());
    expect(acks(h)).toEqual(["a", "b"]);

    const h2 = harness({ synth: false });
    const hook2 = mount(h2);
    server(hook2, { type: "say", id: "x", text: "Hi." });
    expect(acks(h2)).toEqual(["x"]);
    expect(hook2.result.current.snapshot.notice).toMatch(/not available/);
  });

  it("speaks a speakable form but never changes the digits", () => {
    expect(speakableText("We can do 65% or $1,250.50 total.")).toBe("We can do 65 percent or 1,250.50 dollars total.");
    const h = harness();
    const hook = mount(h);
    const u = say(hook, h, "s1", "That is 40% of the balance.");
    expect(u.text).toBe("That is 40 percent of the balance.");
  });
});

describe("utterance retention and stale handlers", () => {
  it("holds the utterance while it plays; a barge makes its late onend a no-op", () => {
    const h = harness();
    const hook = mount(h);
    const u = say(hook, h, "s1");
    expect(hook.result.current.engine.currentUtter).toBe(u);
    act(() => hook.result.current.engine.bargeIn());
    act(() => u.onend?.());
    act(() => u.onerror?.({ error: "interrupted" }));
    expect(acks(h)).toEqual([]);
    expect(h.sent).toContainEqual({ type: "barge_in", spoken_ids: ["s1"] });
  });
});

describe("barge-in and echo guard", () => {
  it("speech right after TTS starts neither barges nor reaches STT", async () => {
    const h = harness();
    const hook = mount(h);
    await micOn(hook);
    const u = say(hook, h, "s1");
    act(() => u.onstart?.());
    act(() => vi.advanceTimersByTime(BARGE_ECHO_GUARD_MS - 100));
    act(() => h.vad.cb!.onSpeechStart());
    act(() => h.vad.cb!.onSpeechEnd(audio));
    expect(types(h)).not.toContain("barge_in");
    expect(h.wavs).toHaveLength(0);
  });

  it("speech over the agent after the guard barges with heard + in-flight ids, and the clip is dropped", async () => {
    const h = harness();
    const hook = mount(h);
    await micOn(hook);
    server(hook, { type: "say", id: "s1", text: "One." });
    server(hook, { type: "say", id: "s2", text: "Two." });
    server(hook, { type: "say", id: "s3", text: "Three." });
    act(() => vi.advanceTimersByTime(20));
    act(() => h.spoken.at(-1)!.onend?.()); // s1 heard
    act(() => vi.advanceTimersByTime(20 + BARGE_ECHO_GUARD_MS + 10)); // s2 playing
    const pauses = h.vad.pause.mock.calls.length;
    const starts = h.vad.start.mock.calls.length;
    await act(async () => h.vad.cb!.onSpeechStart());
    expect(h.sent).toContainEqual({ type: "barge_in", spoken_ids: ["s1", "s2"] });
    expect(hook.result.current.snapshot.speaking).toBe(false);
    // The mixed clip is abandoned at the detector: pause() + start() drops vad-web's
    // in-flight segment, so no onSpeechEnd arrives for it.
    expect(h.vad.pause.mock.calls.length).toBe(pauses + 1);
    expect(h.vad.start.mock.calls.length).toBe(starts + 1);
    expect(h.wavs).toHaveLength(0);

    // The next clean utterance goes to STT.
    act(() => vi.advanceTimersByTime(BARGE_ECHO_GUARD_MS));
    act(() => h.vad.cb!.onSpeechStart());
    expect(hook.result.current.snapshot.interim).toBe(LABEL.listening);
    act(() => h.vad.cb!.onSpeechEnd(audio));
    expect(h.wavs).toHaveLength(1);
    expect(hook.result.current.snapshot.interim).toBe(LABEL.transcribing);
    expect(hook.result.current.mic).toBe("thinking");
  });

  it("a typed reply mid-TTS barges first so the move's bookkeeping lands", () => {
    const h = harness();
    const hook = mount(h);
    say(hook, h, "s1");
    act(() => hook.result.current.engine.beforeRepText());
    expect(types(h)).toEqual(["barge_in"]);
    expect(hook.result.current.snapshot.waiting).toBe(true);
  });
});

describe("listening cues", () => {
  it("shows Loading mic… until VAD is ready, then Listening… without waiting for speech", async () => {
    let release!: () => void;
    const h = harness({ vadGate: new Promise<void>((r) => (release = r)) });
    const hook = mount(h);
    let started!: Promise<void>;
    act(() => {
      started = hook.result.current.engine.startMic();
    });
    expect(hook.result.current.snapshot.interim).toBe(LABEL.loading);
    expect(hook.result.current.mic).toBe("listening");
    await act(async () => {
      release();
      await started;
    });
    expect(h.vad.start).toHaveBeenCalled();
    expect(hook.result.current.snapshot.interim).toBe(LABEL.listening);
  });

  it("browser STT shows Listening… as soon as recognition is armed", async () => {
    const h = harness();
    const hook = mount(h, "browser");
    await micOn(hook);
    const rec = FakeRec.all.at(-1)!;
    act(() => rec.onstart?.());
    expect(hook.result.current.snapshot.interim).toBe(LABEL.listening);
  });

  it("stt_error in server mode falls back to browser recognition", async () => {
    const h = harness();
    const hook = mount(h);
    await micOn(hook);
    server(hook, { type: "stt_error", message: "down" });
    expect(hook.result.current.snapshot.sttMode).toBe("browser");
    expect(FakeRec.all.length).toBe(1);
    expect(hook.result.current.snapshot.notice).toMatch(/browser speech recognition/);
  });
});

describe("browser STT during TTS", () => {
  it("stops recognition for the agent's turn, drops speakback, and re-arms after it", async () => {
    const h = harness();
    const hook = mount(h, "browser");
    await micOn(hook);
    const first = FakeRec.all.at(-1)!;
    const u = say(hook, h, "s1", "Would 65% work?");
    expect(first.aborted).toBe(1);
    act(() => first.final("sixty five percent work")); // detached: ignored
    expect(h.repTexts).toEqual([]);
    act(() => u.onend?.());
    act(() => vi.advanceTimersByTime(BARGE_ECHO_GUARD_MS + BROWSER_STT_SETTLE_MS + 50));
    const second = FakeRec.all.at(-1)!;
    expect(second).not.toBe(first);
    expect(second.started).toBe(1);
    act(() => second.final("Forty percent."));
    expect(h.repTexts).toEqual(["Forty percent."]);
  });
});

describe("Phase 21 voice latency client", () => {
  it("pins vad-web 0.0.22 + onnxruntime 1.14.0 with the tuned VAD settings", () => {
    expect(VAD_WEB_VERSION).toBe("0.0.22");
    expect(ONNX_RUNTIME_VERSION).toBe("1.14.0");
    expect(VAD_OPTIONS).toEqual({
      positiveSpeechThreshold: 0.35,
      negativeSpeechThreshold: 0.2,
      redemptionFrames: 8,
      preSpeechPadFrames: 10,
      minSpeechFrames: 3,
    });
  });

  it("sends vad_end_to_first_audio_ms once, from onstart, not when the reply is queued", async () => {
    const h = harness();
    const hook = mount(h);
    await micOn(hook);
    act(() => h.vad.cb!.onSpeechStart());
    act(() => h.vad.cb!.onSpeechEnd(audio));
    act(() => vi.advanceTimersByTime(500));
    server(hook, { type: "say", id: "s1", text: "One." });
    server(hook, { type: "say", id: "s2", text: "Two." });
    act(() => vi.advanceTimersByTime(20));
    expect(types(h)).not.toContain("timing");
    act(() => vi.advanceTimersByTime(80));
    act(() => h.spoken.at(-1)!.onstart?.());
    const timing = h.sent.filter((e) => e.type === "timing");
    expect(timing).toEqual([{ type: "timing", turn: 3, vad_end_to_first_audio_ms: 600 }]);
    expect(h.onsets).toEqual([[3, 100]]);
    act(() => h.spoken.at(-1)!.onend?.());
    act(() => vi.advanceTimersByTime(20));
    act(() => h.spoken.at(-1)!.onstart?.());
    expect(h.sent.filter((e) => e.type === "timing")).toHaveLength(1);
    expect(h.onsets).toHaveLength(1);
  });

  it("plays a local, number-free backchannel after 1.2 s with no reply; never sent or acked", async () => {
    expect(BACKCHANNEL_TEXT).toBe("One moment.");
    expect(BACKCHANNEL_AFTER_MS).toBe(1200);
    const h = harness();
    const hook = mount(h);
    await micOn(hook);
    act(() => h.vad.cb!.onSpeechStart());
    act(() => h.vad.cb!.onSpeechEnd(audio));
    act(() => vi.advanceTimersByTime(BACKCHANNEL_AFTER_MS - 1));
    expect(h.spoken).toHaveLength(0);
    act(() => vi.advanceTimersByTime(1));
    expect(h.spoken.map((u) => u.text)).toEqual([BACKCHANNEL_TEXT]);
    expect(/\d/.test(BACKCHANNEL_TEXT)).toBe(false);
    expect(hook.result.current.engine.backchannelPlaying).toBe(true);
    act(() => h.spoken[0]!.onend?.());
    expect(h.sent).toEqual([]); // no text, no sentence_done: not part of the agent's turn
  });

  it("cancels the backchannel when the reply arrives first", async () => {
    const h = harness();
    const hook = mount(h);
    await micOn(hook);
    act(() => h.vad.cb!.onSpeechStart());
    act(() => h.vad.cb!.onSpeechEnd(audio));
    act(() => vi.advanceTimersByTime(800));
    say(hook, h, "s1", "Thanks.");
    act(() => vi.advanceTimersByTime(2000));
    expect(h.spoken.map((u) => u.text)).toEqual(["Thanks."]);
  });

  it("prefers natural voices and applies the pick to each utterance", () => {
    expect(PREFERRED_VOICES.slice(0, 2)).toEqual(["Google US English", "Samantha"]);
    expect(PREFERRED_VOICES.some((v) => v.startsWith("Microsoft Aria"))).toBe(true);
    const fred = { name: "Fred", lang: "en-US" };
    const aria = { name: "Microsoft Aria Online (Natural) - English (United States)", lang: "en-US" };
    expect(pickVoice([fred, aria])).toBe(aria);
    expect(pickVoice([fred])).toBe(fred);
    expect(pickVoice([{ name: "Thomas", lang: "fr-FR" }])).toBeNull();

    const h = harness();
    h.voices = [fred, { name: "Samantha", lang: "en-US" }];
    const hook = mount(h);
    const u = say(hook, h, "s1");
    expect(u.voice?.name).toBe("Samantha");
    expect(u.lang).toBe("en-US");
  });
});

describe("autoplay and WAV", () => {
  it("a passive (autoplay) call neither speaks nor acks", () => {
    const h = harness();
    const hook = mount(h);
    act(() => hook.result.current.engine.reset(true));
    say(hook, h, "s1");
    expect(h.spoken).toHaveLength(0);
    expect(h.sent).toEqual([]);
  });

  it("encodes 16 kHz mono 16-bit WAV", () => {
    const wav = encodeWav(new Float32Array([0, 1, -1, 0.5]), 16000);
    const v = new DataView(wav);
    const ascii = (o: number) => String.fromCharCode(...new Uint8Array(wav, o, 4));
    expect([ascii(0), ascii(8), ascii(12), ascii(36)]).toEqual(["RIFF", "WAVE", "fmt ", "data"]);
    expect(v.getUint32(24, true)).toBe(16000);
    expect(v.getUint16(22, true)).toBe(1);
    expect(v.getUint16(34, true)).toBe(16);
    expect(v.getUint32(40, true)).toBe(8);
    expect([v.getInt16(44, true), v.getInt16(46, true), v.getInt16(48, true)]).toEqual([0, 32767, -32768]);
  });
});
