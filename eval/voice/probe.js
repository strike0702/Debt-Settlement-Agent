/*
 * Voice harness page probe (eval/voice). Injected before any page script via
 * CDP Page.addScriptToEvaluateOnNewDocument; window.__VH_CFG is set by the
 * driver in the same init script.
 *
 * Logs, with epoch-ms timestamps (performance.timeOrigin + now):
 *   ws_in / ws_out  call-socket frames (product UI)
 *   agent_audio_on / agent_audio_off  energy on remote WebRTC audio
 *   pc_state  RTCPeerConnection state
 *   ice_pair  selected candidate pair types once connected (Phase 66)
 *   rep_start / rep_end  injected rep clips
 * With __VH_CFG.inject, getUserMedia returns a WebAudio stream the driver
 * feeds through __vh.play(url, id) instead of the microphone.
 * It never changes what the page sends or plays; it only watches.
 */
(() => {
  const cfg = window.__VH_CFG || {};
  const now = () => performance.timeOrigin + performance.now();
  const events = [];
  const log = (kind, data) => events.push({ t: now(), kind, ...(data || {}) });
  const vh = { events, log, cfg };
  window.__vh = vh;
  vh.since = (n) => events.slice(n);

  // ---------------------------------------------------------------- call socket
  const KEEP = ["id", "text", "role", "ids", "turn", "message", "spoken_ids", "source", "phase"];
  const pick = (o) => {
    const out = { type: o.type };
    for (const k of KEEP) if (k in o) out[k] = o[k];
    if (o.type === "latency") Object.assign(out, o);
    if (o.type === "turn_trace") out.timings = o.timings;
    return out;
  };
  const NativeWS = window.WebSocket;
  class ProbeWS extends NativeWS {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", (ev) => {
        if (typeof ev.data !== "string") return;
        try {
          log("ws_in", pick(JSON.parse(ev.data)));
        } catch {
          /* not JSON */
        }
      });
    }
    send(data) {
      if (typeof data === "string") {
        try {
          log("ws_out", pick(JSON.parse(data)));
        } catch {
          /* not JSON */
        }
      }
      return super.send(data);
    }
  }
  window.WebSocket = ProbeWS;

  // ---------------------------------------------------------------- remote audio energy
  const tapTrack = (track) => {
    const ctx = new AudioContext();
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    ctx.createMediaStreamSource(new MediaStream([track])).connect(an);
    const buf = new Float32Array(an.fftSize);
    const ON = cfg.audioOn ?? 0.01;
    const HANG_MS = cfg.audioHangMs ?? 250;
    let on = false;
    let lastLoud = 0;
    vh.remoteCtx = ctx;
    setInterval(() => {
      if (ctx.state === "suspended") void ctx.resume();
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      const rms = Math.sqrt(sum / buf.length);
      const t = now();
      if (rms >= ON) {
        if (!on) log("agent_audio_on", { rms });
        on = true;
        lastLoud = t;
      } else if (on && t - lastLoud > HANG_MS) {
        on = false;
        events.push({ t: lastLoud, kind: "agent_audio_off" });
      }
    }, 10);
  };
  const NativePC = window.RTCPeerConnection;
  if (NativePC) {
    window.RTCPeerConnection = class extends NativePC {
      constructor(...args) {
        super(...args);
        this.addEventListener("track", (ev) => {
          if (ev.track.kind === "audio") tapTrack(ev.track);
        });
        // Phase 66: which candidate pair carries the call (relay = TURN).
        this.logPair = async () => {
          const stats = [...(await this.getStats()).values()];
          const pair = stats.find((r) => r.type === "candidate-pair" && r.nominated && r.state === "succeeded");
          const cand = (id) => stats.find((r) => r.id === id) || {};
          if (pair)
            log("ice_pair", {
              local: cand(pair.localCandidateId).candidateType,
              remote: cand(pair.remoteCandidateId).candidateType,
              relay_protocol: cand(pair.localCandidateId).relayProtocol || null,
              rtt_ms: pair.currentRoundTripTime != null ? pair.currentRoundTripTime * 1000 : null,
            });
        };
        this.addEventListener("connectionstatechange", () => {
          log("pc_state", { state: this.connectionState });
          if (this.connectionState === "connected") void this.logPair();
        });
      }
    };
  }

  // ---------------------------------------------------------------- injected mic
  if (cfg.inject) {
    let ctx = null;
    let dest = null;
    const ensure = () => {
      if (!ctx) {
        ctx = new AudioContext({ sampleRate: 48000 });
        dest = ctx.createMediaStreamDestination();
      }
      return ctx;
    };
    const md = navigator.mediaDevices;
    md.getUserMedia = async (constraints) => {
      log("gum", { constraints: JSON.stringify(constraints) });
      ensure();
      if (ctx.state === "suspended") await ctx.resume();
      return dest.stream.clone();
    };
    const decoded = new Map();
    vh.play = async (url, id) => {
      ensure();
      if (ctx.state === "suspended") await ctx.resume();
      let buf = decoded.get(url);
      if (!buf) {
        const bytes = await (await fetch(url)).arrayBuffer();
        buf = await ctx.decodeAudioData(bytes);
        decoded.set(url, buf);
      }
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(dest);
      const lead = 0.05;
      const start = now() + lead * 1000;
      src.start(ctx.currentTime + lead);
      events.push({ t: start, kind: "rep_start", id, dur_ms: buf.duration * 1000 });
      src.onended = () => log("rep_end_cb", { id });
      events.push({ t: start + buf.duration * 1000, kind: "rep_end", id });
      return buf.duration * 1000;
    };
  }

  // ---------------------------------------------------------------- page helpers
  /** Click the first enabled button whose aria-label or text matches. */
  vh.click = (label) => {
    const btn = [...document.querySelectorAll("button")].find(
      (b) => !b.disabled && (b.getAttribute("aria-label") === label || b.textContent.trim() === label),
    );
    if (!btn) return false;
    btn.click();
    return true;
  };
})();
