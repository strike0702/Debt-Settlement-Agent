/**
 * Voice + dashboard client for /ws/call/{call_id}.
 * Small store + subscribe render; mock mode replays the same event shapes.
 */

const TERM_FIELDS = [
  "max_payments",
  "min_payment_cents",
  "payment_structure",
  "first_payment_date",
  "max_segments",
  "max_token_pays",
  "min_payment_tiers",
];

const REP_CARD_TEXT = `NorthPeak Collections (synthetic)

Hidden rules
• Max payments: 8
• Minimum payment: $100
• Structure: even
• Opening ask: 45%
• Floor: 40%

Script to WRAP
1. Max eight payments, minimum one hundred dollars, even payments please.
2. We are looking for a forty five percent settlement.
3. Yes I accept that payment schedule. Agreed.

Do not ask for income, SDA balance, or draft amount.
Use headphones.`;

function createStore(initial) {
  const listeners = new Map();
  let state = structuredClone(initial);
  return {
    get() {
      return state;
    },
    set(patch) {
      const next = { ...state, ...patch };
      const changed = [];
      for (const key of Object.keys(patch)) {
        if (state[key] !== next[key]) changed.push(key);
      }
      state = next;
      for (const key of changed) {
        const set = listeners.get(key);
        if (set) for (const fn of set) fn(state[key], state);
      }
      const all = listeners.get("*");
      if (all) for (const fn of all) fn(state, changed);
    },
    subscribe(key, fn) {
      if (!listeners.has(key)) listeners.set(key, new Set());
      listeners.get(key).add(fn);
      return () => listeners.get(key).delete(fn);
    },
  };
}

const store = createStore({
  view: "rep",
  callId: null,
  connected: false,
  started: false,
  bargeInEnabled: true,
  mock: false,
  micOn: false,
  phase: "OPENING",
  lastIntent: null,
  transcript: [],
  terms: {},
  eval: null,
  latency: [],
  audit: [],
  guards: { blocked: 0, escalate: 0 },
  speaking: false,
  ackedIds: [],
});

const el = {
  viewCaption: document.getElementById("view-caption"),
  viewRep: document.getElementById("view-rep"),
  viewOp: document.getElementById("view-op"),
  btnStart: document.getElementById("btn-start"),
  btnMic: document.getElementById("btn-mic"),
  togBarge: document.getElementById("tog-barge"),
  togMock: document.getElementById("tog-mock"),
  textInput: document.getElementById("text-input"),
  btnSend: document.getElementById("btn-send"),
  heroLabel: document.getElementById("hero-label"),
  heroMain: document.getElementById("hero-main"),
  heroSub: document.getElementById("hero-sub"),
  heroMetrics: document.getElementById("hero-metrics"),
  transcript: document.getElementById("transcript"),
  terms: document.getElementById("terms"),
  schedule: document.getElementById("schedule"),
  schedTitle: document.getElementById("sched-title"),
  repCard: document.getElementById("rep-card"),
  guards: document.getElementById("guards"),
  latency: document.getElementById("latency"),
  audit: document.getElementById("audit"),
};

let ws = null;
let vad = null;
let mockTimer = null;
let mockIdx = 0;
let pendingSayQueue = [];
let speakingIds = new Set();
let vadEndAt = null;
let onnxBase = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.14.0/dist/";
let vadAssetBase = "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.22/dist/";

function money(cents) {
  if (cents == null) return "—";
  return (cents / 100).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
  });
}

function pct(bp) {
  if (bp == null) return "—";
  return `${(bp / 100).toFixed(bp % 100 === 0 ? 0 : 2)}%`;
}

function flash(node) {
  if (!node) return;
  node.classList.remove("changed");
  void node.offsetWidth;
  node.classList.add("changed");
}

function applyView() {
  const view = store.get().view;
  const isRep = view === "rep";
  el.viewRep.setAttribute("aria-pressed", String(isRep));
  el.viewOp.setAttribute("aria-pressed", String(!isRep));
  el.viewCaption.textContent = isRep
    ? "Creditor rep view"
    : "Operator (firm) view";
  document.querySelectorAll(".rep-only").forEach((n) => {
    n.hidden = !isRep;
  });
  document.querySelectorAll(".op-only").forEach((n) => {
    n.hidden = isRep;
  });
  renderHero();
  renderSchedule();
}

function verdictState(s) {
  if (!s.started && !s.eval) return { key: "pending", label: "Pending" };
  if (s.phase === "ESCALATE") return { key: "infeasible", label: "Escalated" };
  if (s.phase === "END" && s.lastIntent === "NO_DEAL_WRAP") {
    return { key: "infeasible", label: "No deal" };
  }
  if (s.eval) {
    if (s.eval.feasible) return { key: "feasible", label: "Feasible" };
    return { key: "infeasible", label: "Infeasible" };
  }
  const missing = Object.values(s.terms).some(
    (t) =>
      ["max_payments", "min_payment_cents", "payment_structure"].includes(t.field) &&
      (t.status === "UNKNOWN" || t.status === "TENTATIVE" || t.status === "CONTRADICTED"),
  );
  if (missing || s.phase === "DISCOVERY" || s.phase === "OPENING") {
    return { key: "needs_info", label: "Needs info" };
  }
  return { key: "pending", label: "Pending" };
}

function renderHero() {
  const s = store.get();
  const isRep = s.view === "rep";
  if (isRep) {
    el.heroLabel.textContent = "Negotiation status";
    const intent = s.lastIntent || "—";
    el.heroMain.textContent = s.phase || "—";
    el.heroMain.dataset.state =
      s.phase === "WRAP" ? "feasible" : s.phase === "ESCALATE" ? "infeasible" : "pending";
    el.heroSub.textContent = `Last agent intent: ${intent}`;
    el.heroMetrics.innerHTML = `
      <div class="metric"><div class="k">Phase</div><div class="v">${s.phase || "—"}</div></div>
      <div class="metric"><div class="k">Intent</div><div class="v">${intent}</div></div>
      <div class="metric"><div class="k">Offer</div><div class="v">${money(s.eval?.offer_total_cents)}</div></div>
      <div class="metric"><div class="k">Settlement</div><div class="v">${pct(s.eval?.agreed_bp)}</div></div>
    `;
  } else {
    const v = verdictState(s);
    el.heroLabel.textContent = "Engine verdict";
    el.heroMain.textContent = v.label;
    el.heroMain.dataset.state = v.key;
    el.heroSub.textContent = s.eval?.shape
      ? `Shape ${s.eval.shape}`
      : s.phase
        ? `Phase ${s.phase}`
        : "No engine result yet";
    const maxBp = s.eval?.max_bp;
    el.heroMetrics.innerHTML = `
      <div class="metric"><div class="k">Offer total</div><div class="v">${money(s.eval?.offer_total_cents)}</div></div>
      <div class="metric"><div class="k">Settlement</div><div class="v">${pct(s.eval?.agreed_bp)}</div></div>
      <div class="metric"><div class="k">Shape</div><div class="v">${s.eval?.shape || "—"}</div></div>
      <div class="metric"><div class="k">Max affordable <span class="badge private">PRIVATE</span></div><div class="v">${pct(maxBp)}</div></div>
    `;
  }
  flash(el.heroMain);
  [...el.heroMetrics.querySelectorAll(".v")].forEach(flash);
}

function renderTranscript() {
  const lines = store.get().transcript;
  el.transcript.innerHTML = lines
    .map(
      (line) => `
      <div class="transcript-line ${line.role}${line.blocked ? " blocked" : ""}">
        <div class="role">${line.role === "agent" ? "Agent" : "Rep"}</div>
        <div class="text">${escapeHtml(line.text)}</div>
      </div>`,
    )
    .join("");
  el.transcript.scrollTop = el.transcript.scrollHeight;
}

function formatTermValue(field, value) {
  if (value == null || value === "") return "—";
  if (field === "min_payment_cents") return money(value);
  if (Array.isArray(value)) return value.length ? JSON.stringify(value) : "[]";
  return String(value);
}

function renderTerms() {
  const terms = store.get().terms;
  const rows = TERM_FIELDS.map((field) => {
    const t = terms[field] || {
      field,
      value: null,
      status: "UNKNOWN",
      evidence: [],
    };
    const quote = t.evidence?.length ? t.evidence[t.evidence.length - 1].quote : "";
    return `
      <tr title="${escapeAttr(quote ? `Evidence: ${quote}` : "No evidence yet")}">
        <td>${escapeHtml(field)}</td>
        <td class="mono">${escapeHtml(formatTermValue(field, t.value))}</td>
        <td><span class="chip ${escapeAttr(t.status)}">${escapeHtml(t.status)}</span></td>
      </tr>`;
  }).join("");
  el.terms.innerHTML = `
    <table class="terms">
      <thead><tr><th>Field</th><th>Value</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderSchedule() {
  const s = store.get();
  const ev = s.eval;
  const isRep = s.view === "rep";
  el.schedTitle.textContent = isRep ? "Proposed schedule" : "Engine schedule";
  if (!ev || !ev.rows || !ev.rows.length) {
    el.schedule.innerHTML = `<div class="hint">${
      ev && ev.feasible === false
        ? "No feasible schedule under current terms."
        : "No schedule proposed yet."
    }</div>`;
    return;
  }
  if (isRep) {
    el.schedule.innerHTML = `
      <div class="hint" style="margin-bottom:8px">Offer ${money(ev.offer_total_cents)} at ${pct(ev.agreed_bp)}</div>
      <table class="sched">
        <thead><tr><th>Date</th><th>Creditor payment</th></tr></thead>
        <tbody>
          ${ev.rows
            .map(
              (r) => `<tr>
                <td class="mono">${escapeHtml(r.date)}</td>
                <td class="mono">${money(r.creditor_payment_cents)}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>`;
  } else {
    el.schedule.innerHTML = `
      <div class="hint" style="margin-bottom:8px">
        ${ev.feasible ? "Feasible" : "Infeasible"} · ${escapeHtml(ev.shape || "—")} ·
        max affordable ${pct(ev.max_bp)} <span class="badge private">PRIVATE</span>
      </div>
      <table class="sched">
        <thead><tr><th>Date</th><th>Creditor</th><th>Program fee</th><th>Bank</th><th>Balance</th></tr></thead>
        <tbody>
          ${ev.rows
            .map(
              (r) => `<tr>
                <td class="mono">${escapeHtml(r.date)}</td>
                <td class="mono">${money(r.creditor_payment_cents)}</td>
                <td class="mono">${money(r.program_fee_cents)}</td>
                <td class="mono">${money(r.bank_fee_cents)}</td>
                <td class="mono">${money(r.balance_cents)}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>`;
  }
}

function renderGuards() {
  const g = store.get().guards;
  el.guards.innerHTML = `
    <div class="guard-row"><span>NLG blocks</span><span class="mono">${g.blocked}</span></div>
    <div class="guard-row"><span>Escalations</span><span class="mono">${g.escalate}</span></div>`;
}

function renderLatency() {
  const rows = store.get().latency.slice(-8).reverse();
  if (!rows.length) {
    el.latency.innerHTML = `<div class="hint">No turns yet.</div>`;
    return;
  }
  el.latency.innerHTML = rows
    .map(
      (r) => `
      <div class="latency-row">
        <span>Turn ${r.turn ?? "—"}</span>
        <span class="mono">stt ${fmtMs(r.stt_ms)} · nlu ${fmtMs(r.nlu_ms)} · pol ${fmtMs(r.policy_ms)} · nlg ${fmtMs(r.nlg_ms)} · Σ ${fmtMs(r.server_total_ms)}</span>
      </div>`,
    )
    .join("");
}

function renderAudit() {
  const rows = store.get().audit.slice(-20).reverse();
  if (!rows.length) {
    el.audit.innerHTML = `<div class="hint">No audit events yet.</div>`;
    return;
  }
  el.audit.innerHTML = rows
    .map(
      (r) => `
      <div class="audit-row">
        <div class="meta">${escapeHtml(r.ts || "")} · ${escapeHtml(r.actor)} · ${escapeHtml(r.event)}</div>
        <div class="mono">${escapeHtml(JSON.stringify(r.payload ?? {}))}</div>
      </div>`,
    )
    .join("");
}

function fmtMs(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${Math.round(v)}ms`;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttr(s) {
  return escapeHtml(s).replaceAll("'", "&#39;");
}

function applyEvent(msg) {
  if (!msg || !msg.type) return;
  const s = store.get();
  switch (msg.type) {
    case "transcript": {
      const transcript = [
        ...s.transcript,
        {
          role: msg.role,
          text: msg.text,
          spoken: !!msg.spoken,
          sentence_id: msg.sentence_id,
          blocked: !!msg.blocked,
        },
      ];
      store.set({ transcript });
      renderTranscript();
      break;
    }
    case "say":
      if (!s.mock) enqueueSay(msg.id, msg.text);
      break;
    case "belief": {
      const terms = {};
      for (const t of msg.terms || []) terms[t.field] = t;
      store.set({ terms });
      renderTerms();
      renderHero();
      break;
    }
    case "eval":
      store.set({ eval: msg });
      renderHero();
      renderSchedule();
      break;
    case "phase":
      store.set({
        phase: msg.phase || s.phase,
        lastIntent: msg.intent ?? s.lastIntent,
      });
      renderHero();
      break;
    case "blocked":
      store.set({ guards: { ...s.guards, blocked: s.guards.blocked + 1 } });
      renderGuards();
      break;
    case "escalate":
      store.set({ guards: { ...s.guards, escalate: s.guards.escalate + 1 } });
      renderGuards();
      break;
    case "latency": {
      const latency = [...s.latency, msg];
      store.set({ latency });
      renderLatency();
      break;
    }
    case "audit": {
      const audit = [...s.audit, msg];
      store.set({ audit });
      renderAudit();
      break;
    }
    case "error":
      console.warn("server error", msg.message);
      break;
    default:
      break;
  }
}

function resetCallState() {
  store.set({
    started: false,
    phase: "OPENING",
    lastIntent: null,
    transcript: [],
    terms: {},
    eval: null,
    latency: [],
    audit: [],
    guards: { blocked: 0, escalate: 0 },
    ackedIds: [],
    speaking: false,
  });
  pendingSayQueue = [];
  speakingIds = new Set();
  window.speechSynthesis?.cancel();
  renderAll();
}

function renderAll() {
  applyView();
  renderTranscript();
  renderTerms();
  renderSchedule();
  renderGuards();
  renderLatency();
  renderAudit();
  el.repCard.innerHTML = `<div class="title">Your playbook</div>${escapeHtml(REP_CARD_TEXT)}`;
}

function wsUrl(callId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/call/${callId}`;
}

function connectAndStart() {
  resetCallState();
  const callId = crypto.randomUUID();
  store.set({ callId, connected: false, started: false });
  ws = new WebSocket(wsUrl(callId));
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    store.set({ connected: true });
    ws.send(JSON.stringify({ type: "start", scenario: "fixtures/demo" }));
    store.set({ started: true });
    setControlsEnabled(true);
  };
  ws.onmessage = (ev) => {
    try {
      applyEvent(JSON.parse(ev.data));
    } catch (err) {
      console.warn(err);
    }
  };
  ws.onclose = () => {
    store.set({ connected: false });
    setControlsEnabled(false);
  };
}

function setControlsEnabled(on) {
  el.textInput.disabled = !on;
  el.btnSend.disabled = !on;
  el.btnMic.disabled = !on;
}

function sendText() {
  const text = el.textInput.value.trim();
  if (!text) return;
  el.textInput.value = "";
  if (store.get().mock) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "text", text }));
}

function sendJson(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(obj));
}

function enqueueSay(id, text) {
  pendingSayQueue.push({ id, text });
  if (!store.get().speaking) drainSayQueue();
}

function drainSayQueue() {
  if (!pendingSayQueue.length) {
    store.set({ speaking: false });
    return;
  }
  const next = pendingSayQueue.shift();
  store.set({ speaking: true });
  speakingIds.add(next.id);
  const utter = new SpeechSynthesisUtterance(next.text);
  utter.rate = 1.05;
  if (vadEndAt != null) {
    const ms = performance.now() - vadEndAt;
    sendJson({
      type: "timing",
      turn: store.get().latency.at(-1)?.turn ?? null,
      vad_end_to_first_audio_ms: ms,
    });
    vadEndAt = null;
  }
  utter.onend = () => {
    sendJson({ type: "sentence_done", id: next.id });
    const acked = [...store.get().ackedIds, next.id];
    store.set({ ackedIds: acked });
    speakingIds.delete(next.id);
    drainSayQueue();
  };
  utter.onerror = () => {
    speakingIds.delete(next.id);
    drainSayQueue();
  };
  window.speechSynthesis.speak(utter);
}

function bargeIn() {
  if (!store.get().bargeInEnabled) return;
  if (!store.get().speaking && !pendingSayQueue.length) return;
  window.speechSynthesis.cancel();
  pendingSayQueue = [];
  const spoken = [...store.get().ackedIds];
  store.set({ speaking: false });
  speakingIds.clear();
  sendJson({ type: "barge_in", spoken_ids: spoken });
}

function floatTo16BitPCM(float32Array) {
  const buffer = new ArrayBuffer(float32Array.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < float32Array.length; i++) {
    let s = Math.max(-1, Math.min(1, float32Array[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

function encodeWav(samples, sampleRate = 16000) {
  const pcm = floatTo16BitPCM(samples);
  const buffer = new ArrayBuffer(44 + pcm.byteLength);
  const view = new DataView(buffer);
  writeString(view, 0, "RIFF");
  view.setUint32(4, 36 + pcm.byteLength, true);
  writeString(view, 8, "WAVE");
  writeString(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(view, 36, "data");
  view.setUint32(40, pcm.byteLength, true);
  new Uint8Array(buffer, 44).set(new Uint8Array(pcm));
  return buffer;
}

function writeString(view, offset, str) {
  for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
}

async function startMic() {
  if (store.get().mock) return;
  const { MicVAD } = await import(
    "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.22/+esm"
  );
  vad = await MicVAD.new({
    onnxWASMBasePath: onnxBase,
    baseAssetPath: vadAssetBase,
    getStream: async () =>
      navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      }),
    onSpeechStart: () => {
      if (store.get().speaking || pendingSayQueue.length) bargeIn();
    },
    onSpeechEnd: (audio) => {
      vadEndAt = performance.now();
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const wav = encodeWav(audio, 16000);
      ws.send(wav);
    },
  });
  vad.start();
  store.set({ micOn: true });
  el.btnMic.textContent = "Mic on";
}

async function stopMic() {
  if (vad) {
    vad.pause();
    vad.destroy();
    vad = null;
  }
  store.set({ micOn: false });
  el.btnMic.textContent = "Mic off";
}

function startMock() {
  resetCallState();
  store.set({ mock: true, started: true, connected: true });
  setControlsEnabled(false);
  el.btnMic.disabled = true;
  mockIdx = 0;
  runMockStep();
}

function stopMock() {
  if (mockTimer) clearTimeout(mockTimer);
  mockTimer = null;
  mockIdx = 0;
  store.set({ mock: false, started: false, connected: false });
}

function runMockStep() {
  const script = window.MOCK_SCRIPT || [];
  if (mockIdx >= script.length) return;
  const step = script[mockIdx++];
  mockTimer = setTimeout(() => {
    for (const ev of step.events) applyEvent(ev);
    runMockStep();
  }, step.delay || 500);
}

el.viewRep.addEventListener("click", () => {
  store.set({ view: "rep" });
  applyView();
});
el.viewOp.addEventListener("click", () => {
  store.set({ view: "operator" });
  applyView();
});
el.btnStart.addEventListener("click", async () => {
  await stopMic();
  stopMock();
  if (el.togMock.checked) {
    startMock();
  } else {
    connectAndStart();
  }
});
el.btnSend.addEventListener("click", sendText);
el.textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendText();
});
el.togBarge.addEventListener("change", () => {
  store.set({ bargeInEnabled: el.togBarge.checked });
});
el.btnMic.addEventListener("click", async () => {
  if (store.get().micOn) await stopMic();
  else await startMic();
});

const params = new URLSearchParams(location.search);
if (params.get("mock") === "1") {
  el.togMock.checked = true;
}
if (params.get("view") === "operator") {
  store.set({ view: "operator" });
}

renderAll();

if (el.togMock.checked) {
  // Auto-run mock for screenshot / UI iteration.
  startMock();
}
