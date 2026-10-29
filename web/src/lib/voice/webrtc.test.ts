/**
 * `WebRtcVoice` with fake browser deps: signaling to `/voice/offer`, the call
 * state machine (server `voice_state`, mute, push to talk), failures routed to
 * the error line, reconnect after a drop, the echo hint, and `say` never spoken.
 */
import { afterEach, beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { callErrorText } from "@/lib/callError";
import {
  callVoiceState,
  CONNECT_FAILED,
  DISCONNECT_GRACE_MS,
  FINISH_TIMEOUT_MS,
  ICE_WAIT_MS,
  isEcho,
  MAX_RECONNECTS,
  MIC_CONSTRAINTS,
  RECONNECT_DELAY_MS,
  WebRtcVoice,
  type FetchResponse,
  type PeerLike,
  type RtcDeps,
} from "./webrtc";
import type { ServerEvent } from "@/types/protocol";

class FakePeer implements PeerLike {
  connectionState = "new";
  iceGatheringState = "new";
  localDescription: PeerLike["localDescription"] = null;
  onconnectionstatechange: PeerLike["onconnectionstatechange"] = null;
  ontrack: PeerLike["ontrack"] = null;
  remote: { sdp: string; type: string } | null = null;
  closed = false;
  added: MediaStreamTrack[] = [];
  private gather: (() => void) | null = null;
  replaced: (MediaStreamTrack | null)[] = [];
  addTransceiver(track: MediaStreamTrack) {
    this.added.push(track);
    return { sender: { replaceTrack: async (t: MediaStreamTrack | null) => void this.replaced.push(t) } };
  }
  async createOffer() {
    return { sdp: "offer-sdp", type: "offer" };
  }
  async setLocalDescription(d: { sdp?: string; type: string }) {
    this.localDescription = { sdp: d.sdp ?? "", type: d.type };
    queueMicrotask(() => {
      this.iceGatheringState = "complete";
      this.gather?.();
    });
  }
  async setRemoteDescription(d: { sdp: string; type: string }) {
    this.remote = d;
  }
  addEventListener(_t: "icegatheringstatechange", fn: () => void) {
    this.gather = fn;
  }
  close() {
    this.closed = true;
  }
  state(s: string) {
    this.connectionState = s;
    this.onconnectionstatechange?.();
  }
}

function fakeTrack() {
  return {
    enabled: true,
    stopped: false,
    stop() {
      this.stopped = true;
    },
    getSettings: () => ({ echoCancellation: true }),
  };
}

const answer = (status = 200, body: unknown = { sdp: "answer-sdp", type: "answer", pc_id: "pc1" }): FetchResponse => ({
  ok: status < 400,
  status,
  json: async () => body,
});

let peers: FakePeer[];
let track: ReturnType<typeof fakeTrack>;
let deps: RtcDeps & {
  fetch: Mock<RtcDeps["fetch"]>;
  getUserMedia: Mock<RtcDeps["getUserMedia"]>;
  log: Mock<RtcDeps["log"]>;
};
let errors: string[];
let voice: WebRtcVoice;
let clock: number;

beforeEach(() => {
  peers = [];
  errors = [];
  clock = 0;
  track = fakeTrack();
  const stream = { getAudioTracks: () => [track], getTracks: () => [track] } as unknown as MediaStream;
  deps = {
    getUserMedia: vi.fn<RtcDeps["getUserMedia"]>(async () => stream),
    makePeer: () => {
      const p = new FakePeer();
      peers.push(p);
      return p;
    },
    fetch: vi.fn<RtcDeps["fetch"]>(async () => answer()),
    playRemote: vi.fn(),
    now: () => clock,
    log: vi.fn<RtcDeps["log"]>(),
  };
  voice = new WebRtcVoice({ onError: (m) => errors.push(m) }, deps);
});
afterEach(() => vi.useRealTimers());

const ev = (e: ServerEvent) => voice.onServerEvent(e);
const state = () => callVoiceState(voice.snapshot());

async function live() {
  await voice.connect("call 1");
  peers.at(-1)!.state("connected");
}

describe("signaling", () => {
  it("sends the mic with echo cancellation, posts the offer and applies the answer", async () => {
    const p = voice.connect("call 1");
    expect(state()).toBe("connecting");
    await p;
    expect(deps.getUserMedia).toHaveBeenCalledWith(MIC_CONSTRAINTS);
    expect(MIC_CONSTRAINTS.audio).toEqual({ echoCancellation: true, noiseSuppression: true, autoGainControl: true });
    expect(deps.log).toHaveBeenCalledWith("[voice] mic settings", { echoCancellation: true });
    const [url, init] = deps.fetch.mock.calls[0]!;
    expect(url).toBe("/voice/offer?call_id=call%201");
    expect(JSON.parse(init.body)).toEqual({ sdp: "offer-sdp", type: "offer" });
    const pc = peers[0]!;
    expect(pc.added).toEqual([track]);
    expect(pc.remote).toEqual({ sdp: "answer-sdp", type: "answer" });

    const remoteTrack = {} as MediaStreamTrack;
    pc.ontrack?.({ streams: [], track: remoteTrack });
    expect(deps.playRemote).toHaveBeenCalledWith(null, remoteTrack);
    pc.state("connected");
    expect(state()).toBe("listening");
  });

  it("does not wait for ICE gathering past its limit", async () => {
    vi.useFakeTimers();
    const slow = new FakePeer();
    slow.setLocalDescription = async (d) => {
      slow.localDescription = { sdp: d.sdp ?? "", type: d.type };
    };
    deps.makePeer = () => slow;
    const p = voice.connect("c");
    await vi.advanceTimersByTimeAsync(ICE_WAIT_MS - 10);
    expect(deps.fetch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(20);
    await p;
    expect(deps.fetch).toHaveBeenCalledTimes(1);
  });

  it("hang up closes the peer and releases the mic", async () => {
    await live();
    voice.hangUp();
    expect(peers[0]!.closed).toBe(true);
    expect(track.stopped).toBe(true);
    expect(state()).toBe("off");
  });
});

describe("call state", () => {
  it("follows the server: rep speaking with a partial, thinking, agent speaking, listening", async () => {
    await live();
    ev({ type: "voice_state", state: "rep_speaking", text: "we can do" });
    expect(state()).toBe("rep_speaking");
    expect(voice.snapshot().partial).toBe("we can do");
    ev({ type: "transcript", role: "creditor", text: "We can do 45%.", spoken: true, sentence_id: null, blocked: false });
    expect(state()).toBe("thinking");
    expect(voice.snapshot().partial).toBe("");
    ev({ type: "voice_state", state: "agent_speaking", text: "" });
    expect(state()).toBe("agent_speaking");
    ev({ type: "voice_state", state: "listening", text: "" });
    expect(state()).toBe("listening");
    // A turn the agent did not answer still ends thinking.
    ev({ type: "voice_state", state: "thinking", text: "" });
    ev({ type: "turn_done" });
    expect(state()).toBe("listening");
  });

  it("never speaks say lines and ignores the socket while voice is off", async () => {
    const synth = { speak: vi.fn() };
    vi.stubGlobal("speechSynthesis", synth);
    ev({ type: "voice_state", state: "agent_speaking", text: "" });
    expect(state()).toBe("off");
    await live();
    ev({ type: "say", id: "s1", text: "Hello, this is the agent." });
    expect(state()).toBe("listening");
    expect(synth.speak).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("mute and push to talk gate the mic track", async () => {
    await live();
    voice.setMuted(true);
    expect(track.enabled).toBe(false);
    expect(state()).toBe("muted");
    // The agent's audio still shows over a muted mic.
    ev({ type: "voice_state", state: "agent_speaking", text: "" });
    expect(state()).toBe("agent_speaking");
    ev({ type: "voice_state", state: "listening", text: "" });
    voice.setMuted(false);
    expect(track.enabled).toBe(true);

    voice.setPushToTalk(true);
    expect(track.enabled).toBe(false);
    expect(state()).toBe("muted");
    voice.talk(true);
    expect(track.enabled).toBe(true);
    expect(state()).toBe("listening");
    voice.talk(false);
    expect(track.enabled).toBe(false);
  });
});

describe("failures", () => {
  it("mic denied goes to the error line as the blocked-mic copy", async () => {
    deps.getUserMedia.mockRejectedValueOnce(Object.assign(new Error("denied"), { name: "NotAllowedError" }));
    await voice.connect("c");
    expect(state()).toBe("off");
    expect(errors).toHaveLength(1);
    expect(callErrorText({ kind: "error", message: errors[0]! })).toMatch(/^The browser blocked the mic/);
  });

  it("a budget refusal is shown once: the server already sent it on the socket", async () => {
    deps.fetch.mockResolvedValueOnce(answer(429, { error: "Voice is paused: today's voice budget is used up." }));
    await voice.connect("c");
    expect(state()).toBe("off");
    expect(errors).toEqual([]);
    expect(track.stopped).toBe(true);
    expect(callErrorText({ kind: "error", message: "Voice is paused: today's voice budget is used up." })).toMatch(
      /voice budget is used up/,
    );
  });

  it("a refusal the socket did not carry, or a network failure, goes to the error line", async () => {
    deps.fetch.mockResolvedValueOnce(answer(404, { error: "Start the call before turning voice on." }));
    await voice.connect("c");
    deps.fetch.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await voice.connect("c");
    expect(errors).toEqual(["Start the call before turning voice on.", CONNECT_FAILED]);
    expect(callErrorText({ kind: "error", message: CONNECT_FAILED })).toMatch(/^Voice could not connect/);
  });
});

describe("reconnect", () => {
  it("shows reconnecting, waits out a short blip, then starts a fresh connection", async () => {
    vi.useFakeTimers();
    await live();
    peers[0]!.state("disconnected");
    expect(state()).toBe("reconnecting");
    peers[0]!.state("connected");
    expect(state()).toBe("listening");

    peers[0]!.state("disconnected");
    await vi.advanceTimersByTimeAsync(DISCONNECT_GRACE_MS + RECONNECT_DELAY_MS);
    expect(peers[0]!.closed).toBe(true);
    expect(peers).toHaveLength(2);
    peers[1]!.state("connected");
    expect(state()).toBe("listening");
    expect(errors).toEqual([]);
  });

  it("retries while the old session closes (409), then gives up with the error line", async () => {
    vi.useFakeTimers();
    await live();
    deps.fetch.mockResolvedValue(answer(409, { error: "This call already has a voice session." }));
    peers[0]!.state("failed");
    for (let i = 1; i <= MAX_RECONNECTS; i++) await vi.advanceTimersByTimeAsync(RECONNECT_DELAY_MS * i);
    expect(peers).toHaveLength(1 + MAX_RECONNECTS);
    expect(state()).toBe("off");
    expect(errors).toEqual([CONNECT_FAILED]);
  });
});

describe("echo hint", () => {
  it("flags the agent's own words heard while it speaks, and hides them", async () => {
    await live();
    ev({ type: "say", id: "s1", text: "We can offer 45% of the balance over six payments." });
    ev({ type: "voice_state", state: "agent_speaking", text: "" });
    ev({ type: "voice_state", state: "rep_speaking", text: "okay" });
    expect(voice.snapshot().echoHint).toBe(false);
    ev({ type: "voice_state", state: "rep_speaking", text: "of the balance over six" });
    expect(voice.snapshot().echoHint).toBe(true);
    expect(voice.snapshot().partial).toBe("okay");
    voice.dismissEcho();
    ev({ type: "voice_state", state: "rep_speaking", text: "the balance over six payments" });
    expect(voice.snapshot().echoHint).toBe(false);
  });

  it("does not flag the rep repeating terms long after the agent stopped", async () => {
    await live();
    ev({ type: "say", id: "s1", text: "We can offer 45% of the balance." });
    ev({ type: "voice_state", state: "agent_speaking", text: "" });
    ev({ type: "voice_state", state: "listening", text: "" });
    clock += 5000;
    ev({ type: "voice_state", state: "rep_speaking", text: "45% of the balance works" });
    expect(voice.snapshot().echoHint).toBe(false);
    expect(state()).toBe("rep_speaking");
    expect(isEcho("No.", ["No."])).toBe(false);
  });
});

describe("call end (Phase 65)", () => {
  it("keeps the link until the server says the last line has played", async () => {
    await live();
    ev({ type: "voice_state", state: "agent_speaking", text: "" });
    voice.finish();
    expect(track.enabled).toBe(false); // no new turns after the end
    expect(voice.snapshot().link).toBe("live");
    expect(peers[0]!.closed).toBe(false);
    ev({ type: "voice_state", state: "ended", text: "" });
    expect(voice.snapshot().link).toBe("off");
    expect(peers[0]!.closed).toBe(true);
  });

  it("hangs up after the timeout, and does not reconnect when the server closes", async () => {
    vi.useFakeTimers();
    await live();
    voice.finish();
    peers[0]!.state("disconnected");
    expect(voice.snapshot().link).toBe("off");
    expect(peers).toHaveLength(1);

    await live();
    voice.finish();
    vi.advanceTimersByTime(FINISH_TIMEOUT_MS);
    expect(voice.snapshot().link).toBe("off");
  });
});

describe("mic change (Phase 65)", () => {
  it("swaps in a new mic track on the same connection when the device goes away", async () => {
    await live();
    const old = track as unknown as MediaStreamTrack & { onended: (() => void) | null };
    const next = fakeTrack();
    deps.getUserMedia.mockResolvedValueOnce({ getAudioTracks: () => [next], getTracks: () => [next] } as unknown as MediaStream);
    old.onended?.();
    await vi.waitFor(() => expect(peers[0]!.replaced).toEqual([next]));
    expect(track.stopped).toBe(true);
    expect(peers).toHaveLength(1); // no renegotiation
    expect(voice.snapshot().link).toBe("live");
  });
});
