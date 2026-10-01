/**
 * Voice / text demo UI for the debt settlement agent.
 * Rep view: chatbot + dashboard. Operator view: transcript, audit, metrics.
 * Does not own policy — speaks only over the /ws/call protocol.
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

const TERM_LABELS = {
  max_payments: "Max payments",
  min_payment_cents: "Min payment",
  payment_structure: "Structure",
  first_payment_date: "First payment",
  max_segments: "Payment levels",
  max_token_pays: "Token payments",
  min_payment_tiers: "Payment tiers",
};

const PHASE_PLAIN = {
  OPENING: "Opening",
  DISCOVERY: "Gathering terms",
  NEGOTIATE: "Negotiating",
  CONFIRM: "Offer on the table",
  WRAP: "Agreed",
  ESCALATE: "Escalated",
  END: "Ended",
};

const QUIET_AUDIT = new Set(["sentence_done"]);

const onnxBase = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.18.0/dist/";
const vadAssetBase =
  "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.22/dist/";

function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();
  return {
    get: () => state,
    set: (patch) => {
      state = { ...state, ...patch };
      listeners.forEach((fn) => fn(state));
    },
    subscribe: (fn) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

const store = createStore({
  view: "rep",
  callId: null,
  connected: false,
  started: false,
  mock: false,
  micOn: false,
  speaking: false,
  waiting: false,
  bargeInEnabled: true,
  sttMode: "auto",
  sttFallbackNotice: "",
  scenarioId: "easy_deal",
  scenarios: [],
  interim: "",
  transcript: [],
  terms: {},
  eval: null,
  phase: null,
  lastIntent: null,
  guards: { blocked: 0, escalate: 0 },
  latency: [],
  audit: [],
  ackedIds: [],
  repCard: "",
  auditActor: "all",
});

const el = {
  viewCaption: document.getElementById("view-caption"),
  viewRep: document.getElementById("view-rep"),
  viewOp: document.getElementById("view-op"),
  btnStart: document.getElementById("btn-start"),
  btnEnd: document.getElementById("btn-end"),
  btnMic: document.getElementById("btn-mic"),
  btnSend: document.getElementById("btn-send"),
  btnDownload: document.getElementById("btn-download"),
  togBarge: document.getElementById("tog-barge"),
  togMock: document.getElementById("tog-mock"),
  sttMode: document.getElementById("stt-mode"),
  scenarioSelect: document.getElementById("scenario-select"),
  expectedBadge: document.getElementById("expected-badge"),
  textInput: document.getElementById("text-input"),
  notice: document.getElementById("notice"),
  chatLog: document.getElementById("chat-log"),
  transcript: document.getElementById("transcript"),
  terms: document.getElementById("terms"),
  opTerms: document.getElementById("op-terms"),
  schedule: document.getElementById("schedule"),
  opSchedule: document.getElementById("op-schedule"),
  schedTitle: document.getElementById("sched-title"),
  repCard: document.getElementById("rep-card"),
  guards: document.getElementById("guards"),
  latency: document.getElementById("latency"),
  audit: document.getElementById("audit"),
  auditCount: document.getElementById("audit-count"),
  auditFilters: document.getElementById("audit-filters"),
  heroLabel: document.getElementById("hero-label"),
  heroMain: document.getElementById("hero-main"),
  heroSub: document.getElementById("hero-sub"),
  heroMetrics: document.getElementById("hero-metrics"),
  opHeroLabel: document.getElementById("op-hero-label"),
  opHeroMain: document.getElementById("op-hero-main"),
  opHeroSub: document.getElementById("op-hero-sub"),
  opHeroMetrics: document.getElementById("op-hero-metrics"),
  repWorkspace: document.getElementById("rep-workspace"),
  opWorkspace: document.getElementById("op-workspace"),
  splitter: document.getElementById("splitter"),
};

let ws = null;
let vad = null;
let vadEndAt = null;
let pendingSayQueue = [];
const speakingIds = new Set();
let mockTimer = null;
let mockIdx = 0;
let browserRec = null;
let browserActive = false;

function money(cents) {
  if (cents == null) return "—";
  return (Number(cents) / 100).toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
  });
}

function pct(bp) {
  if (bp == null) return "—";
  return `${(Number(bp) / 100).toFixed(Number(bp) % 100 === 0 ? 0 : 2)}%`;
}

function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function flash(node) {
  if (!node) return;
  node.classList.remove("changed");
  void node.offsetWidth;
  node.classList.add("changed");
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

function fmtMs(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${Math.round(v)}ms`;
}

function localTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString(undefined, { hour12: false }) +
    "." +
    String(d.getMilliseconds()).padStart(3, "0");
}

function showNotice(msg) {
  el.notice.textContent = msg || "";
  el.notice.classList.toggle("show", Boolean(msg));
}

function applyView() {
  const view = store.get().view;
  const isRep = view === "rep";
  el.viewRep.setAttribute("aria-pressed", String(isRep));
  el.viewOp.setAttribute("aria-pressed", String(!isRep));
  el.viewCaption.textContent = isRep
    ? "Creditor rep view"
    : "Operator (firm) view";
  el.repWorkspace.hidden = !isRep;
  el.opWorkspace.hidden = isRep;
  renderHero();
  renderSchedule();
  renderTerms();
  renderTranscript();
}

function verdictState(s) {
  if (!s.started && !s.eval) return { key: "pending", label: "Pending" };
  if (s.phase === "ESCALATE") return { key: "infeasible", label: "Escalated" };
  if (s.phase === "END" && s.lastIntent === "NO_DEAL_WRAP") {
    return { key: "infeasible", label: "No deal" };
  }
  if (s.phase === "WRAP") return { key: "feasible", label: "Deal pending approval" };
  if (s.eval) {
    if (s.eval.feasible) return { key: "feasible", label: "Feasible" };
    return { key: "infeasible", label: "Infeasible" };
  }
  if (s.phase === "DISCOVERY" || s.phase === "OPENING") {
    return { key: "needs_info", label: "Needs info" };
  }
  return { key: "pending", label: "Pending" };
}

function renderHero() {
  const s = store.get();
  const plain = PHASE_PLAIN[s.phase] || s.phase || "—";
  const intent = s.lastIntent || "—";

  el.heroMain.textContent = plain;
  el.heroMain.dataset.state =
    s.phase === "WRAP"
      ? "feasible"
      : s.phase === "ESCALATE" || (s.phase === "END" && intent === "NO_DEAL_WRAP")
        ? "infeasible"
        : s.phase === "DISCOVERY" || s.phase === "OPENING"
          ? "needs_info"
          : "pending";
  el.heroSub.textContent = `Last agent intent: ${intent}`;
  el.heroMetrics.innerHTML = `
    <div class="metric"><div class="k">Phase</div><div class="v">${escapeHtml(plain)}</div></div>
    <div class="metric"><div class="k">Intent</div><div class="v">${escapeHtml(intent)}</div></div>
    <div class="metric"><div class="k">Offer</div><div class="v">${money(s.eval?.offer_total_cents)}</div></div>
    <div class="metric"><div class="k">Settlement</div><div class="v">${pct(s.eval?.agreed_bp)}</div></div>
  `;

  const v = verdictState(s);
  el.opHeroMain.textContent = v.label;
  el.opHeroMain.dataset.state = v.key;
  el.opHeroSub.textContent = s.eval?.shape
    ? `Shape ${s.eval.shape}`
    : s.phase
      ? `Phase ${s.phase}`
      : "No engine result yet";
  el.opHeroMetrics.innerHTML = `
    <div class="metric"><div class="k">Offer total</div><div class="v">${money(s.eval?.offer_total_cents)}</div></div>
    <div class="metric"><div class="k">Settlement</div><div class="v">${pct(s.eval?.agreed_bp)}</div></div>
    <div class="metric"><div class="k">Shape</div><div class="v">${escapeHtml(s.eval?.shape || "—")}</div></div>
    <div class="metric"><div class="k">Max affordable <span class="badge private">PRIVATE</span></div><div class="v">${pct(s.eval?.max_bp)}</div></div>
  `;
  flash(el.heroMain);
  flash(el.opHeroMain);
}

function renderChat() {
  const s = store.get();
  const lines = s.transcript;
  const parts = lines.map((line) => {
    const who = line.role === "agent" ? "Agent" : "Rep";
    const meta = [];
    if (line.intent) meta.push(`<span class="chip intent">${escapeHtml(line.intent)}</span>`);
    if (line.ms) meta.push(`<span>${escapeHtml(line.ms)}</span>`);
    return `
      <div class="bubble ${line.role}${line.blocked ? " blocked" : ""}">
        <div class="who">${who}</div>
        <div>${escapeHtml(line.text)}</div>
        ${meta.length ? `<div class="meta">${meta.join("")}</div>` : ""}
      </div>`;
  });
  if (s.interim) {
    parts.push(`
      <div class="bubble rep interim">
        <div class="who">Rep (listening)</div>
        <div>${escapeHtml(s.interim)}</div>
      </div>`);
  }
  if (s.waiting) {
    parts.push(`
      <div class="bubble agent typing">
        <div class="who">Agent</div>
        <div>Thinking…</div>
      </div>`);
  }
  el.chatLog.innerHTML = parts.join("");
  el.chatLog.scrollTop = el.chatLog.scrollHeight;
}

function renderTranscript() {
  const lines = store.get().transcript;
  el.transcript.innerHTML = lines.length
    ? lines
        .map(
          (line, i) => `
      <div class="transcript-block ${line.role}${line.blocked ? " blocked" : ""}">
        <div class="head">
          <span class="role">${line.role === "agent" ? "Agent" : "Rep"}</span>
          <span class="hint">#${i + 1}</span>
          ${line.intent ? `<span class="chip intent">${escapeHtml(line.intent)}</span>` : ""}
          ${line.ms ? `<span class="hint mono">${escapeHtml(line.ms)}</span>` : ""}
        </div>
        <div class="text">${escapeHtml(line.text)}</div>
      </div>`,
        )
        .join("")
    : `<div class="hint">No turns yet.</div>`;
  el.transcript.scrollTop = el.transcript.scrollHeight;
  renderChat();
}

function formatTermValue(field, value) {
  if (value == null || value === "") return "—";
  if (field === "min_payment_cents") return money(value);
  if (field === "first_payment_date") return formatDate(value);
  if (Array.isArray(value)) return value.length ? JSON.stringify(value) : "[]";
  if (field === "payment_structure") {
    return String(value).charAt(0).toUpperCase() + String(value).slice(1);
  }
  return String(value);
}

function termsTableHtml(terms) {
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
        <td>${escapeHtml(TERM_LABELS[field] || field)}</td>
        <td class="mono">${escapeHtml(formatTermValue(field, t.value))}</td>
        <td><span class="chip ${escapeAttr(t.status)}">${escapeHtml(t.status)}</span></td>
      </tr>`;
  }).join("");
  return `
    <table class="terms">
      <thead><tr><th>Field</th><th>Value</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderTerms() {
  const html = termsTableHtml(store.get().terms);
  el.terms.innerHTML = html;
  el.opTerms.innerHTML = html;
}

function scheduleHtml(ev, isRep) {
  if (!ev || !ev.rows || !ev.rows.length) {
    return `<div class="hint">${
      ev && ev.feasible === false
        ? "No feasible schedule under current terms."
        : "No schedule proposed yet."
    }</div>`;
  }
  const rows = ev.rows.filter((r) => (r.creditor_payment_cents || 0) > 0);
  if (isRep) {
    return `
      <div class="hint" style="margin-bottom:8px">Offer ${money(ev.offer_total_cents)} at ${pct(ev.agreed_bp)}</div>
      <table class="sched">
        <thead><tr><th>Date</th><th>Creditor payment</th></tr></thead>
        <tbody>
          ${rows
            .map(
              (r) => `<tr>
                <td class="mono">${escapeHtml(formatDate(r.date))}</td>
                <td class="mono">${money(r.creditor_payment_cents)}</td>
              </tr>`,
            )
            .join("")}
        </tbody>
      </table>`;
  }
  return `
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
              <td class="mono">${escapeHtml(formatDate(r.date))}</td>
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

function renderSchedule() {
  const s = store.get();
  el.schedTitle.textContent =
    s.view === "rep" ? "Proposed schedule" : "Engine schedule";
  el.schedule.innerHTML = scheduleHtml(s.eval, true);
  el.opSchedule.innerHTML = scheduleHtml(s.eval, false);
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

function summarizeAudit(r) {
  const p = r.payload || {};
  switch (r.event) {
    case "observe":
      return `${p.field}: ${p.old_value} → ${p.new_value} (${p.new_status}) ${p.quote ? `'${p.quote}'` : ""}`;
    case "confirm_readback":
      return `${p.field}: ${p.old_status} → ${p.new_status}`;
    case "decide":
      return `${p.intent}${p.reason ? ` because ${p.reason}` : ""}`;
    case "affordability":
      return `max_bp=${p.max_bp ?? "null"}, feasible=${p.n_feasible ?? 0}`;
    case "rescue_check":
      return `ask ${p.ask_bp} bp, within_guardrail=${p.within_guardrail}`;
    case "alt_first_payment_date":
      return `requested ${p.requested} → alt ${p.alt ?? "none"}`;
    case "turn_complete":
      return `${p.intent}: ${(p.sentences || []).join(" / ")}`;
    case "analysis":
      return `stance=${p.stance}, terms=${(p.terms || []).length}, nlu=${fmtMs(p.nlu_ms)}`;
    case "utterance":
      return p.text || "";
    case "call_started":
      return `scenario=${p.scenario_id}${p.rebased_as_of ? ` rebased ${p.rebased_as_of}` : ""}`;
    case "call_ended":
      return `by ${p.by}`;
    case "stt_error":
      return p.message || "STT unavailable";
    default:
      return r.event;
  }
}

function renderAudit() {
  const s = store.get();
  const actor = s.auditActor;
  const rows = s.audit
    .filter((r) => !QUIET_AUDIT.has(r.event))
    .filter((r) => actor === "all" || r.actor === actor)
    .slice()
    .reverse();
  el.auditCount.textContent = `${rows.length} events`;
  if (!rows.length) {
    el.audit.innerHTML = `<div class="hint">No audit events yet.</div>`;
    return;
  }
  // Group by turn-ish: batch consecutive same-minute chunks under first turn_complete seen nearby
  el.audit.innerHTML = rows
    .map((r) => {
      const summary = summarizeAudit(r);
      const pretty = JSON.stringify(r.payload ?? {}, null, 2);
      return `
        <details class="audit-group">
          <summary>
            <span>${escapeHtml(localTime(r.ts))} · ${escapeHtml(r.actor)} · ${escapeHtml(r.event)}</span>
            <span class="hint">${escapeHtml(summary).slice(0, 80)}</span>
          </summary>
          <div class="audit-row">
            <div class="meta">${escapeHtml(summary)}</div>
            <pre>${escapeHtml(pretty)}</pre>
          </div>
        </details>`;
    })
    .join("");
}

function renderAll() {
  renderHero();
  renderTranscript();
  renderTerms();
  renderSchedule();
  renderGuards();
  renderLatency();
  renderAudit();
  if (el.repCard) {
    el.repCard.textContent = store.get().repCard || "Select a scenario to load the playbook.";
  }
  showNotice(store.get().sttFallbackNotice);
}

function resetCallState() {
  pendingSayQueue = [];
  speakingIds.clear();
  window.speechSynthesis?.cancel();
  store.set({
    callId: null,
    connected: false,
    started: false,
    speaking: false,
    waiting: false,
    interim: "",
    transcript: [],
    terms: {},
    eval: null,
    phase: null,
    lastIntent: null,
    guards: { blocked: 0, escalate: 0 },
    latency: [],
    audit: [],
    ackedIds: [],
    sttFallbackNotice: "",
  });
  renderAll();
}

function wsUrl(callId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/call/${callId}`;
}

function setControlsEnabled(on) {
  el.textInput.disabled = !on;
  el.btnSend.disabled = !on;
  el.btnMic.disabled = !on;
  el.btnEnd.disabled = !on;
  el.btnDownload.disabled = !on || !store.get().callId;
}

function connectAndStart() {
  resetCallState();
  const callId = crypto.randomUUID();
  const scenarioId = el.scenarioSelect.value || store.get().scenarioId || "easy_deal";
  store.set({ callId, connected: false, started: false, scenarioId });
  ws = new WebSocket(wsUrl(callId));
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    store.set({ connected: true });
    ws.send(JSON.stringify({ type: "start", scenario_id: scenarioId }));
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
    store.set({ connected: false, waiting: false });
    setControlsEnabled(false);
    stopBrowserRec();
  };
}

function sendText(textOverride, source) {
  const text = (textOverride ?? el.textInput.value).trim();
  if (!text) return;
  if (textOverride == null) el.textInput.value = "";
  if (store.get().mock) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  store.set({ waiting: true, interim: "" });
  renderChat();
  const payload = { type: "text", text };
  if (source) payload.source = source;
  ws.send(JSON.stringify(payload));
}

function sendJson(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(obj));
}

function endChat() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  stopMic();
  stopBrowserRec();
  sendJson({ type: "end" });
  store.set({ waiting: true });
  renderChat();
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

function applyEvent(msg) {
  const type = msg.type;
  const s = store.get();

  if (type === "transcript") {
    const line = {
      role: msg.role,
      text: msg.text,
      blocked: Boolean(msg.blocked),
      intent: msg.role === "agent" ? s.lastIntent : null,
    };
    store.set({ transcript: [...s.transcript, line], interim: "" });
    renderTranscript();
    return;
  }
  if (type === "say") {
    enqueueSay(msg.id, msg.text);
    return;
  }
  if (type === "belief") {
    const terms = {};
    for (const t of msg.terms || []) terms[t.field] = t;
    store.set({ terms });
    renderTerms();
    return;
  }
  if (type === "phase") {
    store.set({
      phase: msg.phase,
      lastIntent: msg.intent || s.lastIntent,
    });
    renderHero();
    return;
  }
  if (type === "eval") {
    store.set({ eval: msg });
    renderHero();
    renderSchedule();
    return;
  }
  if (type === "blocked") {
    store.set({
      guards: { ...s.guards, blocked: s.guards.blocked + 1 },
    });
    renderGuards();
    return;
  }
  if (type === "escalate") {
    store.set({
      guards: { ...s.guards, escalate: s.guards.escalate + 1 },
    });
    renderGuards();
    return;
  }
  if (type === "latency") {
    const row = { ...msg };
    const ms = `Σ ${fmtMs(row.server_total_ms)}`;
    const transcript = store.get().transcript.map((line, idx, arr) => {
      if (idx === arr.length - 1 && line.role === "agent") {
        return { ...line, ms, intent: store.get().lastIntent };
      }
      return line;
    });
    store.set({
      latency: [...store.get().latency, row],
      transcript,
    });
    renderLatency();
    renderTranscript();
    return;
  }
  if (type === "audit") {
    store.set({ audit: [...store.get().audit, msg] });
    renderAudit();
    return;
  }
  if (type === "agreement") {
    renderHero();
    return;
  }
  if (type === "stt_error") {
    const mode = store.get().sttMode;
    if (mode === "auto" || mode === "server") {
      store.set({
        sttMode: "browser",
        sttFallbackNotice:
          "Server speech recognition is down — switched to browser STT.",
      });
      el.sttMode.value = "browser";
      showNotice(store.get().sttFallbackNotice);
      if (store.get().micOn) {
        stopMic().then(() => startBrowserRec());
      }
    } else {
      showNotice(`STT error: ${msg.message || "unavailable"}`);
    }
    return;
  }
  if (type === "error") {
    showNotice(msg.message || "Error");
    store.set({ waiting: false });
    renderChat();
    return;
  }
  if (type === "turn_done") {
    store.set({ waiting: false });
    renderChat();
  }
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

function effectiveSttMode() {
  return store.get().sttMode;
}

async function startMic() {
  if (store.get().mock) return;
  const mode = effectiveSttMode();
  if (mode === "browser") {
    startBrowserRec();
    store.set({ micOn: true });
    el.btnMic.textContent = "Mic on";
    return;
  }
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
      store.set({ waiting: true });
      renderChat();
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
  stopBrowserRec();
  store.set({ micOn: false, interim: "" });
  el.btnMic.textContent = "Mic off";
  renderChat();
}

function startBrowserRec() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    showNotice("Browser speech recognition is not available in this browser.");
    return;
  }
  stopBrowserRec();
  browserRec = new SR();
  browserRec.continuous = true;
  browserRec.interimResults = true;
  browserRec.onstart = () => {
    browserActive = true;
  };
  browserRec.onspeechstart = () => {
    if (store.get().speaking || pendingSayQueue.length) bargeIn();
  };
  browserRec.onresult = (event) => {
    let interim = "";
    let finalText = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const res = event.results[i];
      if (res.isFinal) finalText += res[0].transcript;
      else interim += res[0].transcript;
    }
    if (interim) {
      store.set({ interim });
      renderChat();
    }
    if (finalText.trim()) {
      store.set({ interim: "" });
      sendText(finalText.trim(), "browser_stt");
    }
  };
  browserRec.onerror = (ev) => {
    if (ev.error !== "no-speech" && ev.error !== "aborted") {
      showNotice(`Browser STT: ${ev.error}`);
    }
  };
  browserRec.onend = () => {
    browserActive = false;
    if (store.get().micOn && effectiveSttMode() === "browser") {
      try {
        browserRec.start();
      } catch {
        /* ignore restart races */
      }
    }
  };
  browserRec.start();
  browserActive = true;
}

function stopBrowserRec() {
  if (browserRec) {
    try {
      browserRec.onend = null;
      browserRec.stop();
    } catch {
      /* ignore */
    }
    browserRec = null;
  }
  browserActive = false;
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

async function loadScenarios() {
  try {
    const res = await fetch("/scenarios");
    const list = await res.json();
    store.set({ scenarios: list });
    el.scenarioSelect.innerHTML = list
      .map(
        (s) =>
          `<option value="${escapeAttr(s.id)}">${escapeHtml(s.title)}</option>`,
      )
      .join("");
    const current = store.get().scenarioId;
    if (list.some((s) => s.id === current)) el.scenarioSelect.value = current;
    updateExpectedBadge();
    await loadRepCard(el.scenarioSelect.value);
  } catch (err) {
    console.warn(err);
  }
}

function updateExpectedBadge() {
  const id = el.scenarioSelect.value;
  const meta = store.get().scenarios.find((s) => s.id === id);
  if (!meta) {
    el.expectedBadge.hidden = true;
    return;
  }
  el.expectedBadge.hidden = false;
  el.expectedBadge.textContent = meta.expected;
  el.expectedBadge.title = meta.description || "";
}

async function loadRepCard(id) {
  try {
    const res = await fetch(`/scenarios/${encodeURIComponent(id)}/rep_card`);
    const data = await res.json();
    store.set({ repCard: data.markdown || "" });
    el.repCard.textContent = data.markdown || "";
  } catch {
    el.repCard.textContent = "";
  }
}

async function downloadLog() {
  const id = store.get().callId;
  if (!id) return;
  const res = await fetch(`/calls/${encodeURIComponent(id)}/export`);
  if (!res.ok) {
    showNotice("No log available yet for this call.");
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `call-${id}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

function initSplitter() {
  const saved = localStorage.getItem("dsa_chat_width");
  if (saved) {
    document.documentElement.style.setProperty("--chat-width", saved);
  }
  let dragging = false;
  el.splitter.addEventListener("pointerdown", (e) => {
    dragging = true;
    el.splitter.setPointerCapture(e.pointerId);
  });
  el.splitter.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const rect = el.repWorkspace.getBoundingClientRect();
    const pct = Math.min(75, Math.max(30, ((e.clientX - rect.left) / rect.width) * 100));
    const value = `${pct}%`;
    document.documentElement.style.setProperty("--chat-width", value);
    localStorage.setItem("dsa_chat_width", value);
  });
  el.splitter.addEventListener("pointerup", () => {
    dragging = false;
  });
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
  showNotice("");
  if (el.togMock.checked) startMock();
  else connectAndStart();
});
el.btnEnd.addEventListener("click", endChat);
el.btnSend.addEventListener("click", () => sendText());
el.textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendText();
});
el.togBarge.addEventListener("change", () => {
  store.set({ bargeInEnabled: el.togBarge.checked });
});
el.sttMode.addEventListener("change", async () => {
  store.set({ sttMode: el.sttMode.value, sttFallbackNotice: "" });
  showNotice("");
  if (store.get().micOn) {
    await stopMic();
    await startMic();
  }
});
el.btnMic.addEventListener("click", async () => {
  if (store.get().micOn) await stopMic();
  else await startMic();
});
el.scenarioSelect.addEventListener("change", async () => {
  store.set({ scenarioId: el.scenarioSelect.value });
  updateExpectedBadge();
  await loadRepCard(el.scenarioSelect.value);
});
el.btnDownload.addEventListener("click", downloadLog);
el.auditFilters.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-actor]");
  if (!btn) return;
  el.auditFilters.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  store.set({ auditActor: btn.dataset.actor });
  renderAudit();
});

const params = new URLSearchParams(location.search);
if (params.get("mock") === "1") el.togMock.checked = true;
if (params.get("view") === "operator") store.set({ view: "operator" });
if (params.get("scenario")) {
  store.set({ scenarioId: params.get("scenario") });
}

initSplitter();
applyView();
renderAll();
loadScenarios();

if (el.togMock.checked) startMock();
