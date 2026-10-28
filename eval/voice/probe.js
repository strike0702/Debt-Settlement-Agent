/*
 * Voice harness page probe (eval/voice). Injected before any page script via
 * CDP Page.addScriptToEvaluateOnNewDocument; window.__VH_CFG is set by the
 * driver in the same init script.
 *
 * Logs, with epoch-ms timestamps (performance.timeOrigin + now):
 *   ws_in / ws_out / ws_out_wav  call-socket frames (legacy console)
 *   tts_speak / tts_start / tts_end / tts_error / tts_cancel  speechSynthesis
 *   agent_audio_on / agent_audio_off  energy on remote WebRTC audio (spike)
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
    if (o.type === "timing") Object.assign(out, o);
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
      } else {
        log("ws_out_wav", { bytes: data.byteLength ?? data.size ?? 0 });
      }
      return super.send(data);
    }
  }
  window.WebSocket = ProbeWS;

  // ---------------------------------------------------------------- speechSynthesis
  const synth = window.speechSynthesis;
  if (synth) {
    const speak = synth.speak.bind(synth);
    const cancel = synth.cancel.bind(synth);
    synth.speak = (u) => {
      const text = u.text;
      log("tts_speak", { text });
      u.addEventListener("start", () => log("tts_start", { text }));
      u.addEventListener("end", () => log("tts_end", { text }));
      u.addEventListener("error", (e) => log("tts_error", { text, error: e.error }));
      return speak(u);
    };
    synth.cancel = () => {
      if (synth.speaking || synth.pending) log("tts_cancel", {});
      return cancel();
    };
  }

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
        this.addEventListener("connectionstatechange", () =>
          log("pc_state", { state: this.connectionState }),
        );
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
  /** Set a <select> that has an option with value `value`. */
  vh.select = (value) => {
    const sel = [...document.querySelectorAll("select")].find((s) =>
      [...s.options].some((o) => o.value === value),
    );
    if (!sel) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
    setter.call(sel, value);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  };
})();
