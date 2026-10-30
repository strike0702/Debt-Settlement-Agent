/**
 * WebRTC voice client (the only voice path): mic up, agent audio down, call state.
 *
 * The server (Pipecat + Deepgram) hears, decides barges and speaks; this file
 * only connects the mic to it and shows what the call is doing. It sends the
 * mic over one `RTCPeerConnection` (signaling: `POST /voice/offer?call_id=`),
 * hands the agent's audio track to an `<audio>` element, and folds the
 * socket's `voice_state` frames (live Flux partials, thinking, agent audio on
 * and off) into a snapshot. It never speaks `say` lines. While voice is on the
 * server acks what it played; while voice is off the rep reads the lines, so
 * this client acks them (`io.ack`): the opening waits (voice may still join and
 * speak it) until the rep's first typed line, and after that every line is
 * acked as it arrives. Without those acks a typed reply would barge an
 * "unheard" agent turn and roll it back (Phase 67, DECISIONS D17).
 *
 * Each new peer connection first gets fresh ICE servers from
 * `GET /voice/ice?call_id=` (Cloudflare STUN + short-lived TURN, minted on the
 * server; Render's ingress needs the TURN relay). If that fails the connection
 * still tries without them.
 *
 * `useWebRtcVoice` wraps it for React; tests pass fake `RtcDeps`.
 * Failures go to `RtcIO.onError` as raw messages that `callErrorText` turns
 * into the visible error line; refusals the server already put on the call
 * socket (409, 429, 503) are not repeated.
 */
import type { ServerEvent } from "@/types/protocol";

/** What the call is doing, as the conversation panel shows it. */
export type CallVoiceState =
  | "off"
  | "connecting"
  | "listening"
  | "rep_speaking"
  | "thinking"
  | "agent_speaking"
  | "muted"
  | "reconnecting";

export type Link = "off" | "connecting" | "live" | "reconnecting";
export type Flow = "listening" | "rep_speaking" | "thinking" | "agent_speaking";

export interface RtcSnapshot {
  link: Link;
  flow: Flow;
  /** Flux's live partial while the rep talks; empty otherwise. */
  partial: string;
  muted: boolean;
  pushToTalk: boolean;
  /** Push-to-talk button held. */
  talking: boolean;
  /** The mic heard the agent's own words: suggest headphones. */
  echoHint: boolean;
}

export const MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
};
/** Wait at most this long for ICE gathering before sending the offer. */
export const ICE_WAIT_MS = 2000;
/** A `disconnected` link that has not come back by then is restarted. */
export const DISCONNECT_GRACE_MS = 5000;
/** Fresh connections tried after a drop, spaced `RECONNECT_DELAY_MS × attempt`. */
export const MAX_RECONNECTS = 3;
export const RECONNECT_DELAY_MS = 1500;
/** Rep words this soon after the agent's audio stopped can still be its echo. */
export const ECHO_TAIL_MS = 1500;
/** After the call is over, wait at most this long for the server's `ended`. */
export const FINISH_TIMEOUT_MS = 25000;

export const MIC_BLOCKED = "mic blocked";
export const MIC_UNAVAILABLE = "mic unavailable";
export const CONNECT_FAILED = "voice connection failed";
/** `/voice/offer` statuses whose message the server also sent on the call socket. */
const SOCKET_REPORTED = new Set([409, 429, 503]);

export interface PeerLike {
  connectionState: string;
  iceGatheringState: string;
  localDescription: { sdp: string; type: string } | null;
  onconnectionstatechange: (() => void) | null;
  ontrack: ((ev: { streams: readonly MediaStream[]; track: MediaStreamTrack }) => void) | null;
  addTransceiver(
    track: MediaStreamTrack,
    init: { direction: "sendrecv" },
  ): { sender: { replaceTrack(track: MediaStreamTrack | null): Promise<void> } };
  createOffer(): Promise<{ sdp?: string; type: string }>;
  setLocalDescription(d: { sdp?: string; type: string }): Promise<void>;
  setRemoteDescription(d: { sdp: string; type: string }): Promise<void>;
  addEventListener(type: "icegatheringstatechange", fn: () => void): void;
  close(): void;
}

export interface FetchResponse {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}

/** One `RTCIceServer` as `GET /voice/ice` returns it. */
export interface IceServerLike {
  urls: string | string[];
  username?: string;
  credential?: string;
}

export interface RtcDeps {
  getUserMedia(c: MediaStreamConstraints): Promise<MediaStream>;
  makePeer(iceServers?: IceServerLike[]): PeerLike;
  /** Fresh ICE servers for the call (`GET /voice/ice`); fakes may omit it. */
  iceServers?(callId: string): Promise<IceServerLike[]>;
  fetch(url: string, init: { method: string; headers: Record<string, string>; body: string }): Promise<FetchResponse>;
  /** Play the agent's audio (an `<audio>` element's `srcObject`). */
  playRemote(stream: MediaStream | null, track: MediaStreamTrack): void;
  now(): number;
  log(msg: string, data?: unknown): void;
  /** Subscribe to mic plug / unplug (`devicechange`); returns the unsubscribe. */
  onDeviceChange?(fn: () => void): () => void;
}

export interface RtcIO {
  /** A failure for the visible error line (raw text; `callErrorText` words it). */
  onError(message: string): void;
  /** Text mode: the rep has seen these sentences (`sentence_done` each). */
  ack?(ids: string[]): void;
}

/** One label for the panel: link problems first, then the agent, then the mic. */
export function callVoiceState(s: RtcSnapshot): CallVoiceState {
  if (s.link !== "live") return s.link === "off" ? "off" : s.link;
  if (s.flow === "agent_speaking" || s.flow === "thinking") return s.flow;
  if (!micOpen(s)) return "muted";
  return s.flow;
}

export const VOICE_LABEL: Record<CallVoiceState, string> = {
  off: "Voice off",
  connecting: "Connecting",
  listening: "Listening",
  rep_speaking: "Rep speaking",
  thinking: "Thinking",
  agent_speaking: "Agent speaking",
  muted: "Muted",
  reconnecting: "Reconnecting",
};

function micOpen(s: Pick<RtcSnapshot, "muted" | "pushToTalk" | "talking">): boolean {
  return !s.muted && (!s.pushToTalk || s.talking);
}

function words(text: string): string[] {
  return text.toLowerCase().match(/[a-z0-9']+/g) ?? [];
}

/**
 * The rep's words repeat the agent's: a run of up to 4 consecutive rep words
 * (at least 3) appears in one recent agent sentence. Phase 62 measured about
 * one echo turn per minute of agent audio on laptop speakers.
 */
export function isEcho(repText: string, agentTexts: readonly string[]): boolean {
  const rep = words(repText);
  if (rep.length < 3) return false;
  const n = Math.min(4, rep.length);
  const agent = agentTexts.map((t) => ` ${words(t).join(" ")} `);
  for (let i = 0; i + n <= rep.length; i++) {
    const run = ` ${rep.slice(i, i + n).join(" ")} `;
    if (agent.some((a) => a.includes(run))) return true;
  }
  return false;
}

function micError(err: unknown): string {
  const name = (err as { name?: string } | null)?.name ?? "";
  if (name === "NotAllowedError" || name === "SecurityError") return `${MIC_BLOCKED}: ${name}`;
  const msg = String((err as { message?: string } | null)?.message ?? err ?? "");
  return `${MIC_UNAVAILABLE}: ${name || msg}`.slice(0, 160);
}

function stopTracks(stream: MediaStream | null): void {
  stream?.getTracks().forEach((t) => t.stop());
}

const INITIAL: RtcSnapshot = {
  link: "off",
  flow: "listening",
  partial: "",
  muted: false,
  pushToTalk: false,
  talking: false,
  echoHint: false,
};

export class WebRtcVoice {
  private snap: RtcSnapshot = INITIAL;
  private listeners = new Set<() => void>();
  private gen = 0;
  private callId: string | null = null;
  private stream: MediaStream | null = null;
  private track: MediaStreamTrack | null = null;
  private pc: PeerLike | null = null;
  private attempts = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private loggedSettings = false;
  private echoDismissed = false;
  private agentTexts: string[] = [];
  private agentQuietAt = -Infinity;
  /** The call is over; the link stays up until the last line has played. */
  private finishing = false;
  private finishTimer: ReturnType<typeof setTimeout> | null = null;
  private sender: { replaceTrack(track: MediaStreamTrack | null): Promise<void> } | null = null;
  private offDevices: (() => void) | null = null;
  /** Text mode: `say` ids shown but not yet acked (the opening, before the rep's first line). */
  private unacked: string[] = [];
  private repSpoke = false;
  /** Autoplay: the server acks every line itself. */
  private passive = false;

  constructor(
    private io: RtcIO,
    private deps: RtcDeps,
  ) {}

  setIO(io: RtcIO): void {
    this.io = io;
  }

  // ------------------------------------------------------------ store

  snapshot = (): RtcSnapshot => this.snap;

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private set(patch: Partial<RtcSnapshot>): void {
    const next = { ...this.snap, ...patch };
    if ((Object.keys(patch) as (keyof RtcSnapshot)[]).every((k) => next[k] === this.snap[k])) return;
    this.snap = next;
    this.listeners.forEach((fn) => fn());
  }

  // ------------------------------------------------------------ connect

  /** Turn voice on for a call that has started on the socket. */
  async connect(callId: string): Promise<void> {
    if (this.snap.link !== "off") return;
    const gen = ++this.gen;
    this.callId = callId;
    this.attempts = 0;
    this.agentTexts = [];
    this.agentQuietAt = -Infinity;
    this.set({ link: "connecting", flow: "listening", partial: "", talking: false });
    let stream: MediaStream;
    try {
      stream = await this.deps.getUserMedia(MIC_CONSTRAINTS);
    } catch (err) {
      if (gen !== this.gen) return;
      this.set({ link: "off" });
      this.io.onError(micError(err));
      return;
    }
    if (gen !== this.gen) {
      stopTracks(stream);
      return;
    }
    this.stream = stream;
    this.track = stream.getAudioTracks()[0] ?? null;
    if (!this.track) {
      this.fail(gen, `${MIC_UNAVAILABLE}: no audio track`);
      return;
    }
    if (!this.loggedSettings) {
      // Once per page: confirms the browser's echo cancellation is really on.
      this.loggedSettings = true;
      this.deps.log("[voice] mic settings", this.track.getSettings?.());
    }
    this.watchMic(gen);
    this.applyMic();
    await this.negotiate(gen);
  }

  /** Follow a mic change: the device was unplugged, or the default device changed. */
  private watchMic(gen: number): void {
    if (this.track) this.track.onended = () => void this.swapMic(gen);
    this.offDevices ??= this.deps.onDeviceChange?.(() => {
      const t = this.track;
      if (t && (t.readyState === "ended" || t.getSettings?.().deviceId === "default")) void this.swapMic(this.gen);
    }) ?? null;
  }

  /** New mic track on the same peer connection (no renegotiation). */
  private async swapMic(gen: number): Promise<void> {
    if (gen !== this.gen || this.snap.link === "off") return;
    let stream: MediaStream;
    try {
      stream = await this.deps.getUserMedia(MIC_CONSTRAINTS);
    } catch (err) {
      if (gen === this.gen) this.fail(gen, micError(err));
      return;
    }
    const track = stream.getAudioTracks()[0] ?? null;
    if (gen !== this.gen || !track) {
      stopTracks(stream);
      return;
    }
    const old = this.stream;
    this.stream = stream;
    this.track = track;
    this.applyMic();
    this.watchMic(gen);
    await this.sender?.replaceTrack(track).catch(() => undefined);
    stopTracks(old);
    this.deps.log("[voice] mic changed", track.getSettings?.());
  }

  private async negotiate(gen: number): Promise<void> {
    const track = this.track;
    if (!track || gen !== this.gen) return;
    const servers = await this.deps.iceServers?.(this.callId ?? "").catch(() => undefined);
    if (gen !== this.gen || this.track !== track) return;
    const pc = this.deps.makePeer(servers);
    this.pc = pc;
    this.sender = pc.addTransceiver(track, { direction: "sendrecv" }).sender;
    pc.ontrack = (ev) => this.deps.playRemote(ev.streams[0] ?? null, ev.track);
    pc.onconnectionstatechange = () => this.onPeerState(pc, gen);
    let res: FetchResponse;
    let body: { sdp?: string; type?: string; error?: string };
    try {
      await pc.setLocalDescription(await pc.createOffer());
      await this.gathered(pc);
      const local = pc.localDescription;
      if (gen !== this.gen || this.pc !== pc || !local) return;
      res = await this.deps.fetch(`/voice/offer?call_id=${encodeURIComponent(this.callId ?? "")}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp: local.sdp, type: local.type }),
      });
      body = ((await res.json().catch(() => ({}))) ?? {}) as typeof body;
    } catch {
      if (gen === this.gen && this.pc === pc) this.fail(gen, CONNECT_FAILED);
      return;
    }
    if (gen !== this.gen || this.pc !== pc) return;
    if (!res.ok) {
      // A reconnect can race the old server session's teardown (409): try again.
      if (res.status === 409 && this.attempts > 0) {
        this.restart(gen);
        return;
      }
      this.fail(gen, body.error ?? CONNECT_FAILED, SOCKET_REPORTED.has(res.status));
      return;
    }
    try {
      await pc.setRemoteDescription({ sdp: body.sdp ?? "", type: body.type ?? "answer" });
    } catch {
      if (gen === this.gen && this.pc === pc) this.fail(gen, CONNECT_FAILED);
    }
  }

  private gathered(pc: PeerLike): Promise<void> {
    return new Promise((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      const t = setTimeout(resolve, ICE_WAIT_MS);
      pc.addEventListener("icegatheringstatechange", () => {
        if (pc.iceGatheringState !== "complete") return;
        clearTimeout(t);
        resolve();
      });
    });
  }

  private onPeerState(pc: PeerLike, gen: number): void {
    if (gen !== this.gen || this.pc !== pc) return;
    const st = pc.connectionState;
    if (this.finishing && st !== "connected" && st !== "connecting" && st !== "new") {
      this.hangUp(); // the server ended voice: no reconnect
      return;
    }
    if (st === "connected") {
      this.clearTimer();
      this.attempts = 0;
      this.unacked = []; // the server speaks and acks whatever is still waiting
      this.set({ link: "live" });
    } else if (st === "disconnected") {
      // ICE often recovers by itself; restart only if it does not.
      this.set({ link: "reconnecting" });
      this.clearTimer();
      this.timer = setTimeout(() => this.restart(gen), DISCONNECT_GRACE_MS);
    } else if (st === "failed") {
      this.restart(gen);
    }
  }

  /** New peer connection after a drop; gives up after `MAX_RECONNECTS`. */
  private restart(gen: number): void {
    if (gen !== this.gen) return;
    this.clearTimer();
    this.closePeer();
    if (this.attempts >= MAX_RECONNECTS) {
      this.fail(gen, CONNECT_FAILED);
      return;
    }
    this.attempts += 1;
    this.set({ link: "reconnecting", flow: "listening", partial: "" });
    this.timer = setTimeout(() => void this.negotiate(gen), RECONNECT_DELAY_MS * this.attempts);
  }

  private fail(gen: number, message: string, reported = false): void {
    if (gen !== this.gen) return;
    this.hangUp();
    if (!reported) this.io.onError(message);
  }

  private clearTimer(): void {
    if (this.timer != null) clearTimeout(this.timer);
    this.timer = null;
  }

  private closePeer(): void {
    const pc = this.pc;
    this.pc = null;
    if (!pc) return;
    pc.onconnectionstatechange = null;
    pc.ontrack = null;
    try {
      pc.close();
    } catch {
      /* ignore */
    }
  }

  /** Turn voice off: close the peer and release the mic. The call goes on in text. */
  hangUp(): void {
    this.gen += 1;
    this.clearTimer();
    this.finishing = false;
    if (this.finishTimer != null) clearTimeout(this.finishTimer);
    this.finishTimer = null;
    this.closePeer();
    this.sender = null;
    this.offDevices?.();
    this.offDevices = null;
    if (this.track) this.track.onended = null;
    stopTracks(this.stream);
    this.stream = null;
    this.track = null;
    this.set({ link: "off", flow: "listening", partial: "", talking: false });
  }

  /**
   * The call is over: close the mic but keep the link until the server says its
   * last line has played (`voice_state: ended`), or `FINISH_TIMEOUT_MS`. Hanging
   * up at once cut the closing line (live test 2026-10-29).
   */
  finish(): void {
    if (this.snap.link === "off" || this.finishing) return;
    this.finishing = true;
    this.applyMic();
    this.finishTimer = setTimeout(() => this.hangUp(), FINISH_TIMEOUT_MS);
  }

  /** New call: voice off, and the headphone hint may show again. */
  reset(autoplay = false): void {
    this.hangUp();
    this.passive = autoplay;
    this.unacked = [];
    this.repSpoke = false;
    this.echoDismissed = false;
    this.set({ echoHint: false });
  }

  /** The rep typed or clicked a reply: with voice off, ack what they have read first. */
  beforeRepText(): void {
    this.repSpoke = true;
    // The server ignores client acks while its voice session is attached.
    if (this.snap.link === "live" || this.unacked.length === 0) return;
    const ids = this.unacked;
    this.unacked = [];
    this.io.ack?.(ids);
  }

  dispose(): void {
    this.hangUp();
    this.listeners.clear();
  }

  // ------------------------------------------------------------ mic

  setMuted(muted: boolean): void {
    this.set({ muted });
    this.applyMic();
  }

  setPushToTalk(on: boolean): void {
    this.set({ pushToTalk: on, talking: false });
    this.applyMic();
  }

  /** Push-to-talk button pressed (true) or released (false). */
  talk(down: boolean): void {
    if (!this.snap.pushToTalk) return;
    this.set({ talking: down });
    this.applyMic();
  }

  private applyMic(): void {
    // A disabled track sends silence; the server keeps Flux fed either way.
    if (this.track) this.track.enabled = micOpen(this.snap) && !this.finishing;
  }

  dismissEcho(): void {
    this.echoDismissed = true;
    this.set({ echoHint: false });
  }

  // ------------------------------------------------------------ server events

  /** Feed every socket frame here; ignored while voice is off. */
  onServerEvent(ev: ServerEvent): void {
    if (ev.type === "say") {
      // Never spoken here (the server's TTS speaks it); kept for the echo check.
      this.agentTexts = [...this.agentTexts.slice(-5), ev.text];
      if (this.passive || this.snap.link === "live") return;
      // While voice is connecting it may still speak the line; hold it until the link is up.
      if (this.repSpoke && this.snap.link === "off") this.io.ack?.([ev.id]);
      else this.unacked.push(ev.id);
      return;
    }
    if (this.snap.link === "off") return;
    switch (ev.type) {
      case "voice_state":
        this.onVoiceState(ev.state, ev.text ?? "");
        return;
      case "transcript":
        if (ev.role === "creditor") this.set({ flow: "thinking", partial: "" });
        return;
      case "turn_done":
      case "error":
        if (this.snap.flow === "thinking") this.set({ flow: "listening" });
        return;
      default:
        return;
    }
  }

  private onVoiceState(state: Flow | "ended", text: string): void {
    if (state === "ended") {
      this.hangUp();
      return;
    }
    const now = this.deps.now();
    const wasAgent = this.snap.flow === "agent_speaking";
    if (state === "rep_speaking") {
      const agentAudible = wasAgent || now - this.agentQuietAt < ECHO_TAIL_MS;
      if (agentAudible && isEcho(text, this.agentTexts)) {
        if (!this.echoDismissed) this.set({ echoHint: true });
        return; // the agent's own voice: not the rep's words
      }
      // Overlapping words while the agent talks: show them, keep the agent's state.
      this.set(wasAgent ? { partial: text } : { flow: "rep_speaking", partial: text });
      return;
    }
    if (wasAgent && state !== "agent_speaking") this.agentQuietAt = now;
    this.set({ flow: state, partial: "" });
  }
}
