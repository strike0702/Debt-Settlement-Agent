/**
 * Voice engine: mic capture, STT routing, TTS playback, barge-in, echo guard.
 *
 * A framework-free port of the voice half of the old `app/static/app.js`. It
 * talks to the call socket only through `VoiceIO` and to the browser only
 * through `VoiceDeps`, so tests drive it with fakes. `useVoice` wraps it for
 * React. It never decides what the agent says; it only plays `say` lines and
 * acks them with `sentence_done` (the server commits a move's effects once
 * every sentence is acked or barged).
 *
 * Invariants worth knowing before editing:
 * - Every `say` gets exactly one `sentence_done` (spoken, failed, or no TTS),
 *   unless a barge-in drops it; then `barge_in.spoken_ids` names what was heard,
 *   including the sentence in flight.
 * - `ttsGen` bumps on every cancel, so a stale `onend`/`onerror` never acks.
 * - Speech that starts while the agent talks (or within the echo guard) can
 *   barge, but its clip is "contaminated" and never sent to STT.
 * - Browser STT is stopped for the whole agent turn (it would hear the TTS).
 * - The backchannel ("One moment.") is local: never sent, never acked.
 */
import type { ClientEvent, ServerEvent } from "@/types/protocol";
import { isBenignTtsError, pickVoice, speakableText, type VoiceLike } from "./speech";
import { MIC_CONSTRAINTS, type VadCallbacks, type VadLike } from "./vad";
import { encodeWav } from "./wav";

export type SttMode = "server" | "browser";

/** Ignore barge for this long after TTS starts (speaker-onset echo). */
export const BARGE_ECHO_GUARD_MS = 750;
export const BACKCHANNEL_TEXT = "One moment.";
export const BACKCHANNEL_AFTER_MS = 1200;
/** Our own filler must not read as rep speech or barge. */
export const BACKCHANNEL_GUARD_MS = 1500;
/** Let trailing speaker audio die out before browser STT re-arms. */
export const BROWSER_STT_SETTLE_MS = 500;
/** Chrome pauses long utterances unless resumed periodically. */
const TTS_KEEPALIVE_MS = 8000;

export const LABEL = {
  listening: "Listening…",
  transcribing: "Transcribing…",
  loading: "Loading mic…",
} as const;

export interface UtterLike {
  text: string;
  rate: number;
  voice: VoiceLike | null;
  lang: string;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((ev: { error?: string }) => void) | null;
}

export interface SynthLike {
  speak(u: UtterLike): void;
  cancel(): void;
  resume(): void;
  getVoices(): VoiceLike[];
}

export interface RecResultList {
  length: number;
  [i: number]: { isFinal: boolean; 0: { transcript: string } };
}

export interface RecLike {
  continuous: boolean;
  interimResults: boolean;
  onstart: (() => void) | null;
  onspeechstart: (() => void) | null;
  onresult: ((ev: { resultIndex: number; results: RecResultList }) => void) | null;
  onerror: ((ev: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

export interface VoiceDeps {
  now(): number;
  synth: SynthLike | null;
  makeUtterance(text: string): UtterLike;
  getUserMedia(c: MediaStreamConstraints): Promise<MediaStream>;
  createVad(stream: MediaStream, callbacks: VadCallbacks): Promise<VadLike>;
  SpeechRecognition: (new () => RecLike) | null;
}

export interface VoiceIO {
  /** Send a JSON event if the socket is open; otherwise drop it. */
  sendJson(ev: ClientEvent): void;
  /** Send a WAV clip; false when the socket is not open. */
  sendWav(wav: ArrayBuffer): boolean;
  /** A browser-STT final: the app's normal rep-text path (barge, then `text`). */
  sendRepText(text: string, source: "browser_stt"): void;
  /** Turn number of the latest agent move (from `phase`), for `timing`. */
  currentTurn(): number | null;
  /** `say` received → speech audibly started, for the latency waterfall. */
  onTtsOnset?(turn: number, ms: number): void;
}

export interface VoiceSnapshot {
  micOn: boolean;
  /** TTS is playing or queued. */
  speaking: boolean;
  /** Rep finished; waiting for the agent's `turn_done`. */
  waiting: boolean;
  /** Listening… / Transcribing… / Loading mic… / a live browser-STT partial. */
  interim: string;
  notice: string;
  sttMode: SttMode;
}

const PLACEHOLDERS: readonly string[] = Object.values(LABEL);

function stopTracks(stream: MediaStream | null | undefined): void {
  stream?.getTracks?.().forEach((t) => {
    try {
      t.stop();
    } catch {
      /* ignore */
    }
  });
}

function destroyVad(instance: VadLike | null): void {
  if (!instance) return;
  try {
    instance.pause();
  } catch {
    /* ignore */
  }
  stopTracks(instance.stream);
  try {
    instance.destroy();
  } catch {
    /* ignore */
  }
}

async function resumeAudio(instance: VadLike | null): Promise<void> {
  const ctx = instance?.audioContext;
  // The async import loses the user gesture, leaving the AudioContext suspended:
  // the mic looks dead for the first turns unless resumed.
  if (ctx && ctx.state === "suspended") {
    try {
      await ctx.resume();
    } catch {
      /* ignore */
    }
  }
}

function isMicPermissionError(err: unknown): boolean {
  const name = (err as { name?: string } | null)?.name ?? "";
  const msg = String((err as { message?: string } | null)?.message ?? err ?? "");
  return (
    name === "NotAllowedError" ||
    name === "NotFoundError" ||
    /Permission denied|not allowed|Requested device not found/i.test(msg)
  );
}

function shortMicError(err: unknown): string {
  if (isMicPermissionError(err)) {
    return "The browser blocked the mic. Allow Microphone from the site icon in the address bar, then try again.";
  }
  const raw = String((err as { message?: string } | null)?.message ?? err ?? "unknown error");
  return raw.length > 160 ? `${raw.slice(0, 157)}…` : raw;
}

export class VoiceEngine {
  private snap: VoiceSnapshot;
  private listeners = new Set<() => void>();

  private queue: { id: string; text: string }[] = [];
  private speakingIds = new Set<string>();
  private ackedIds: string[] = [];
  private ttsGen = 0;
  /** Kept alive on purpose: Chrome garbage-collects a dropped utterance mid-playback. */
  currentUtter: UtterLike | null = null;
  private backchannelUtter: UtterLike | null = null;
  private backchannelTimer: ReturnType<typeof setTimeout> | null = null;
  private bargeSuppressUntil = 0;
  private utteranceContaminated = false;
  private browserIgnoreResults = false;
  private browserPausedForTts = false;
  private vad: VadLike | null = null;
  private micStream: MediaStream | null = null;
  private micGen = 0;
  private rec: RecLike | null = null;
  private vadEndAt: number | null = null;
  private turnSayAt: number | null = null;
  private passive = false;
  private cachedVoice: VoiceLike | null | undefined = undefined;

  constructor(
    private io: VoiceIO,
    private deps: VoiceDeps,
    sttMode: SttMode = "server",
  ) {
    this.snap = { micOn: false, speaking: false, waiting: false, interim: "", notice: "", sttMode };
  }

  /** Swap the socket side (React passes fresh callbacks each render). */
  setIO(io: VoiceIO): void {
    this.io = io;
  }

  // ------------------------------------------------------------ store

  snapshot = (): VoiceSnapshot => this.snap;

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private set(patch: Partial<VoiceSnapshot>): void {
    const next = { ...this.snap, ...patch };
    if ((Object.keys(patch) as (keyof VoiceSnapshot)[]).every((k) => next[k] === this.snap[k])) return;
    this.snap = next;
    this.listeners.forEach((fn) => fn());
  }

  private agentIsTalking(): boolean {
    return this.snap.speaking || this.queue.length > 0;
  }

  // ------------------------------------------------------------ call lifecycle

  /** New call: drop queued speech and per-call ack history. `passive` = autoplay (no TTS, no acks). */
  reset(passive = false): void {
    this.queue = [];
    this.cancelTts();
    this.cancelBackchannel();
    this.speakingIds.clear();
    this.ackedIds = [];
    this.turnSayAt = null;
    this.vadEndAt = null;
    this.passive = passive;
    this.set({ speaking: false, waiting: false, notice: "" });
  }

  dismissNotice(): void {
    this.set({ notice: "" });
  }

  dispose(): void {
    this.reset();
    void this.stopMic();
    this.listeners.clear();
  }

  /** Feed every server frame here (after the app's reducer). */
  onServerEvent(ev: ServerEvent): void {
    switch (ev.type) {
      case "say":
        this.cancelBackchannel();
        if (this.passive) return; // autoplay auto-acks server-side
        this.enqueueSay(ev.id, ev.text);
        return;
      case "transcript":
        if (ev.role === "creditor") this.set({ interim: "" });
        return;
      case "stt_error":
        this.set({ waiting: false, interim: "" });
        if (this.snap.sttMode === "server") {
          this.fallBackToBrowser("Server speech recognition is down, so the mic switched to browser speech recognition.");
        } else {
          this.set({ notice: `STT error: ${ev.message || "unavailable"}` });
        }
        return;
      case "error":
        this.set({ waiting: false });
        return;
      case "turn_done":
        this.set({ waiting: false });
        this.maybeShowListening();
        return;
      default:
        return;
    }
  }

  /** The rep typed or clicked a reply: barge any agent speech first so its bookkeeping lands. */
  beforeRepText(): void {
    if (this.agentIsTalking()) this.bargeIn();
    this.set({ waiting: true, interim: "" });
  }

  // ------------------------------------------------------------ TTS

  private enqueueSay(id: string, text: string): void {
    if (this.turnSayAt == null) this.turnSayAt = this.deps.now();
    this.queue.push({ id, text });
    if (!this.snap.speaking) this.drainQueue();
  }

  private ack(id: string): void {
    this.io.sendJson({ type: "sentence_done", id });
    this.ackedIds.push(id);
    this.speakingIds.delete(id);
  }

  private cancelTts(): void {
    this.ttsGen += 1;
    this.currentUtter = null;
    try {
      this.deps.synth?.cancel();
    } catch {
      /* ignore */
    }
  }

  private voice(): VoiceLike | null {
    if (this.cachedVoice !== undefined) return this.cachedVoice;
    let voices: VoiceLike[];
    try {
      voices = this.deps.synth?.getVoices() ?? [];
    } catch {
      voices = [];
    }
    if (!voices.length) return null; // still loading: retry on the next utterance
    this.cachedVoice = pickVoice(voices);
    return this.cachedVoice;
  }

  /** Voices load asynchronously; call on `voiceschanged`. */
  voicesChanged(): void {
    this.cachedVoice = undefined;
  }

  applyVoice(u: UtterLike): void {
    const v = this.voice();
    if (v) {
      u.voice = v;
      u.lang = v.lang;
    }
  }

  private stampFirstAudio(): void {
    const now = this.deps.now();
    const turn = this.io.currentTurn();
    if (this.turnSayAt != null) {
      if (turn != null) this.io.onTtsOnset?.(turn, now - this.turnSayAt);
      this.turnSayAt = null;
    }
    // F14: first audio = the reply's first utterance actually starting (onstart).
    if (this.vadEndAt == null) return;
    const ms = now - this.vadEndAt;
    this.vadEndAt = null;
    this.io.sendJson({ type: "timing", turn, vad_end_to_first_audio_ms: ms });
  }

  private drainQueue(): void {
    const next = this.queue.shift();
    if (!next) {
      this.set({ speaking: false });
      this.currentUtter = null;
      this.turnSayAt = null;
      // Re-arm the mic after the whole agent turn, not between sentences.
      this.scheduleListening();
      return;
    }
    // Browser STT must not hear the TTS, or it barges at "65%" and sticks.
    this.pauseBrowserRecForTts();
    const synth = this.deps.synth;
    if (!synth) {
      // No TTS: still ack so the server's pending effects commit.
      this.ack(next.id);
      this.set({ notice: "Speech playback is not available in this browser." });
      this.drainQueue();
      return;
    }
    this.set({ speaking: true });
    this.speakingIds.add(next.id);
    const gen = ++this.ttsGen;
    const utter = this.deps.makeUtterance(speakableText(next.text));
    this.currentUtter = utter;
    this.applyVoice(utter);
    utter.rate = 1.05;
    utter.onstart = () => {
      if (gen !== this.ttsGen) return;
      this.stampFirstAudio();
    };
    const keepAlive = setInterval(() => {
      if (gen !== this.ttsGen) {
        clearInterval(keepAlive);
        return;
      }
      try {
        synth.resume();
      } catch {
        /* ignore */
      }
    }, TTS_KEEPALIVE_MS);
    utter.onend = () => {
      clearInterval(keepAlive);
      if (gen !== this.ttsGen) return;
      this.ack(next.id);
      this.drainQueue();
    };
    utter.onerror = (ev) => {
      clearInterval(keepAlive);
      // A cancel or barge bumped ttsGen: the barge owns the bookkeeping.
      if (gen !== this.ttsGen) return;
      // F06: still ack, or the server's pending move never commits.
      this.ack(next.id);
      if (!isBenignTtsError(ev?.error)) this.set({ notice: "Speech playback failed; continuing the turn." });
      this.drainQueue();
    };
    this.bargeSuppressUntil = this.deps.now() + BARGE_ECHO_GUARD_MS;
    try {
      synth.resume();
    } catch {
      /* ignore */
    }
    // Chrome often drops speak() when it races a prior cancel(); wait one tick.
    setTimeout(() => {
      if (gen !== this.ttsGen || this.currentUtter !== utter) return;
      try {
        synth.speak(utter);
      } catch {
        utter.onerror?.({ error: "synthesis-failed" });
      }
    }, 20);
  }

  private armBackchannel(): void {
    this.cancelBackchannel();
    this.backchannelTimer = setTimeout(() => this.speakBackchannel(), BACKCHANNEL_AFTER_MS);
  }

  private cancelBackchannel(): void {
    if (this.backchannelTimer != null) clearTimeout(this.backchannelTimer);
    this.backchannelTimer = null;
  }

  /** Local filler while the reply is pending. Never sent, never acked: not part of the agent's turn. */
  private speakBackchannel(): void {
    this.backchannelTimer = null;
    const synth = this.deps.synth;
    if (!synth || this.agentIsTalking() || !this.snap.waiting) return;
    const utter = this.deps.makeUtterance(BACKCHANNEL_TEXT);
    this.applyVoice(utter);
    utter.rate = 1.05;
    this.backchannelUtter = utter;
    const release = () => {
      if (this.backchannelUtter === utter) this.backchannelUtter = null;
    };
    utter.onend = release;
    utter.onerror = release;
    this.bargeSuppressUntil = this.deps.now() + BACKCHANNEL_GUARD_MS;
    try {
      synth.speak(utter);
    } catch {
      this.backchannelUtter = null;
    }
  }

  get backchannelPlaying(): boolean {
    return this.backchannelUtter !== null;
  }

  // ------------------------------------------------------------ barge-in

  /** Stop agent speech; tell the server which sentences the rep heard (incl. the one in flight). */
  bargeIn(): void {
    if (!this.agentIsTalking()) return;
    const spoken = [...new Set([...this.ackedIds, ...this.speakingIds])];
    this.queue = [];
    this.cancelTts();
    this.speakingIds.clear();
    this.turnSayAt = null;
    this.set({ speaking: false });
    this.io.sendJson({ type: "barge_in", spoken_ids: spoken });
    // Abandon the mixed clip; the rep's next words start a clean utterance.
    void this.armMicAfterAgent();
  }

  private maybeBargeFromMic(): boolean {
    if (this.browserPausedForTts) return false;
    if (this.deps.now() < this.bargeSuppressUntil) return false;
    if (!this.agentIsTalking()) return false;
    this.bargeIn();
    return true;
  }

  // ------------------------------------------------------------ listening cue

  /** Show "Listening…" as soon as capture is armed, not when speech is first heard. */
  private maybeShowListening(): void {
    if (!this.snap.micOn || this.agentIsTalking() || this.snap.waiting) return;
    if (this.deps.now() < this.bargeSuppressUntil) return;
    if (this.browserPausedForTts) return;
    if (this.snap.sttMode !== "browser" && !this.vad) return; // VAD still loading
    const cur = this.snap.interim;
    if (cur && !PLACEHOLDERS.includes(cur)) return; // keep a live partial
    this.set({ interim: LABEL.listening });
  }

  /** After a short reply the echo guard may still be on: defer re-arming until it ends. */
  private scheduleListening(): void {
    const wait = Math.max(0, this.bargeSuppressUntil - this.deps.now());
    if (wait <= 0) {
      void this.armMicAfterAgent();
      return;
    }
    setTimeout(() => void this.armMicAfterAgent(), wait + 20);
  }

  /**
   * Drop any VAD clip that heard the agent and restart capture. Without this the
   * redemption window keeps the rep's next words inside a contaminated clip that
   * gets discarded, which feels like seconds of deafness.
   */
  private async resetVadCapture(): Promise<void> {
    this.utteranceContaminated = false;
    const instance = this.vad;
    if (!this.snap.micOn || !instance) return;
    try {
      instance.pause();
    } catch {
      /* ignore */
    }
    try {
      instance.start();
    } catch {
      /* ignore */
    }
    await resumeAudio(instance);
  }

  private async armMicAfterAgent(): Promise<void> {
    this.utteranceContaminated = false;
    this.browserIgnoreResults = false;
    if (this.snap.sttMode !== "browser") {
      await this.resetVadCapture();
      this.maybeShowListening();
      return;
    }
    this.browserPausedForTts = false;
    if (!this.snap.micOn) return;
    this.stopBrowserRec();
    setTimeout(() => {
      if (!this.snap.micOn || this.agentIsTalking() || this.snap.sttMode !== "browser") return;
      this.startBrowserRec();
    }, BROWSER_STT_SETTLE_MS);
  }

  // ------------------------------------------------------------ mic

  async setSttMode(mode: SttMode): Promise<void> {
    if (mode === this.snap.sttMode) return;
    this.set({ sttMode: mode, notice: "" });
    if (this.snap.micOn) {
      await this.stopMic();
      await this.startMic();
    }
  }

  async toggleMic(): Promise<void> {
    if (this.snap.micOn) await this.stopMic();
    else await this.startMic();
  }

  private fallBackToBrowser(reason: string): void {
    const wasOn = this.snap.micOn;
    this.teardownVad();
    this.set({ sttMode: "browser", notice: reason });
    if (wasOn) this.startBrowserRec();
  }

  async startMic(): Promise<void> {
    // micOn flips first, so a second click during the VAD load routes to stopMic.
    const gen = ++this.micGen;
    const mode = this.snap.sttMode;
    this.set({ micOn: true, notice: "", interim: mode === "browser" ? LABEL.listening : LABEL.loading });
    if (mode === "browser") {
      this.startBrowserRec();
      return;
    }
    let instance: VadLike | null = null;
    let stream: MediaStream | null = null;
    try {
      stream = await this.deps.getUserMedia(MIC_CONSTRAINTS);
      if (gen !== this.micGen) {
        stopTracks(stream);
        return;
      }
      this.micStream = stream;
      instance = await this.deps.createVad(stream, this.vadCallbacks(gen));
      await resumeAudio(instance);
    } catch (err) {
      if (gen !== this.micGen) return;
      destroyVad(instance);
      stopTracks(stream);
      this.micStream = null;
      if (!isMicPermissionError(err)) {
        // VAD is only the capture path; the browser's recognizer still works.
        this.fallBackToBrowser("Voice detection failed to load, so the mic switched to browser speech recognition. You can also type.");
        return;
      }
      this.set({ notice: shortMicError(err) });
      await this.stopMic(true);
      return;
    }
    if (gen !== this.micGen) {
      // Stopped while loading: tear down what was just created.
      destroyVad(instance);
      stopTracks(stream);
      this.micStream = null;
      return;
    }
    this.vad = instance;
    instance.start();
    await resumeAudio(instance);
    this.maybeShowListening();
  }

  private vadCallbacks(gen: number): VadCallbacks {
    return {
      onSpeechStart: () => {
        if (gen !== this.micGen) return;
        // Speech over the agent can barge, but its audio is not safe to transcribe.
        if (this.agentIsTalking() || this.deps.now() < this.bargeSuppressUntil) {
          this.utteranceContaminated = true;
          if (this.maybeBargeFromMic()) this.set({ interim: LABEL.listening });
          return;
        }
        this.utteranceContaminated = false;
        this.set({ interim: LABEL.listening });
      },
      onVADMisfire: () => {
        if (gen !== this.micGen) return;
        this.utteranceContaminated = false;
        this.maybeShowListening();
      },
      onSpeechEnd: (audio) => {
        if (gen !== this.micGen) return;
        if (this.utteranceContaminated) {
          // TTS bleed or a mixed barge clip: drop it and wait for a clean one.
          this.utteranceContaminated = false;
          this.maybeShowListening();
          return;
        }
        this.vadEndAt = this.deps.now();
        if (!this.io.sendWav(encodeWav(audio, 16000))) return;
        this.set({ waiting: true, interim: LABEL.transcribing });
        this.armBackchannel();
      },
    };
  }

  private teardownVad(): void {
    this.micGen += 1;
    const instance = this.vad;
    this.vad = null;
    destroyVad(instance);
    stopTracks(this.micStream);
    this.micStream = null;
  }

  async stopMic(keepNotice = false): Promise<void> {
    this.teardownVad();
    this.utteranceContaminated = false;
    this.browserIgnoreResults = false;
    this.browserPausedForTts = false;
    this.stopBrowserRec();
    this.set({ micOn: false, interim: "", ...(keepNotice ? {} : { notice: "" }) });
  }

  // ------------------------------------------------------------ browser STT

  private pauseBrowserRecForTts(): void {
    if (this.snap.sttMode !== "browser") return;
    this.browserPausedForTts = true;
    this.browserIgnoreResults = true;
    this.set({ interim: "" });
    this.stopBrowserRec();
  }

  private startBrowserRec(): void {
    const SR = this.deps.SpeechRecognition;
    if (!SR) {
      this.set({ micOn: false, interim: "", notice: "Browser speech recognition is not available in this browser. You can type instead." });
      return;
    }
    // Don't re-arm while the agent's TTS owns the speakers.
    if (this.browserPausedForTts || this.agentIsTalking()) {
      this.browserPausedForTts = true;
      return;
    }
    this.stopBrowserRec();
    const rec = new SR();
    this.rec = rec;
    rec.continuous = true;
    rec.interimResults = true;
    rec.onstart = () => this.maybeShowListening();
    rec.onspeechstart = () => {
      if (this.browserPausedForTts || this.agentIsTalking() || this.deps.now() < this.bargeSuppressUntil) {
        this.browserIgnoreResults = true;
        return;
      }
      this.browserIgnoreResults = false;
      this.set({ interim: LABEL.listening });
    };
    rec.onresult = (event) => {
      if (this.rec !== rec || this.browserPausedForTts) return;
      let interim = "";
      let finalText = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const res = event.results[i]!;
        if (res.isFinal) finalText += res[0].transcript;
        else interim += res[0].transcript;
      }
      // Drop speakback finals that overlapped the agent's TTS.
      if (this.browserIgnoreResults || this.agentIsTalking()) {
        if (finalText.trim()) this.browserIgnoreResults = true;
        return;
      }
      if (interim) this.set({ interim });
      if (finalText.trim()) {
        this.set({ interim: "" });
        this.io.sendRepText(finalText.trim(), "browser_stt");
      }
    };
    rec.onerror = (ev) => {
      if (ev.error !== "no-speech" && ev.error !== "aborted") this.set({ notice: `Browser STT: ${ev.error}` });
    };
    rec.onend = () => {
      // Chrome ends continuous recognition on silence; restart only the live recognizer.
      if (
        this.rec === rec &&
        this.snap.micOn &&
        this.snap.sttMode === "browser" &&
        !this.browserPausedForTts &&
        !this.agentIsTalking()
      ) {
        this.maybeShowListening();
        try {
          rec.start();
        } catch {
          /* restart race */
        }
      }
    };
    rec.start();
    this.maybeShowListening();
  }

  private stopBrowserRec(): void {
    const rec = this.rec;
    if (!rec) return;
    this.rec = null;
    rec.onend = rec.onresult = rec.onspeechstart = rec.onerror = null;
    try {
      rec.stop();
    } catch {
      /* ignore */
    }
    try {
      rec.abort();
    } catch {
      /* ignore */
    }
  }
}
