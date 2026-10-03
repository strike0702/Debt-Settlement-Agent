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
const SCENARIO_KEY = "dsa_scenario_id";
const CUSTOM_KEY = "dsa_custom_scenario";
const CUSTOM_ID = "__custom__";

const onnxBase = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.14.0/dist/";
const vadAssetBase =
  "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.22/dist/";

function savedScenarioId() {
  try {
    return localStorage.getItem(SCENARIO_KEY) || "";
  } catch {
    return "";
  }
}

function rememberScenarioId(id) {
  try {
    if (id) localStorage.setItem(SCENARIO_KEY, id);
    else localStorage.removeItem(SCENARIO_KEY);
  } catch {
    /* ignore quota / private mode */
  }
}

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
  ending: false,
  mock: false,
  micOn: false,
  speaking: false,
  waiting: false,
  bargeInEnabled: true,
  sttMode: "auto",
  sttFallbackNotice: "",
  scenarioId: savedScenarioId(),
  scenarios: [],
  customPayload: null,
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
  btnCall: document.getElementById("btn-call"),
  btnCallOp: document.getElementById("btn-call-op"),
  btnMic: document.getElementById("btn-mic"),
  btnSend: document.getElementById("btn-send"),
  btnDownload: document.getElementById("btn-download"),
  togMock: document.getElementById("tog-mock"),
  sttMode: document.getElementById("stt-mode"),
  scenarioSelect: document.getElementById("scenario-select"),
  expectedBadge: document.getElementById("expected-badge"),
  opControls: document.getElementById("op-controls"),
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
  scenarioBrief: document.getElementById("scenario-brief"),
  briefTitle: document.getElementById("brief-title"),
  repWorkspace: document.getElementById("rep-workspace"),
  opWorkspace: document.getElementById("op-workspace"),
  splitter: document.getElementById("splitter"),
  customCasePanel: document.getElementById("custom-case-panel"),
  customCaseShell: document.getElementById("custom-case-shell"),
  customJson: document.getElementById("custom-json"),
  customStatus: document.getElementById("custom-status"),
  btnLoadTemplate: document.getElementById("btn-load-template"),
  btnApplyCustom: document.getElementById("btn-apply-custom"),
  customFile: document.getElementById("custom-file"),
};

let ws = null;
let vad = null;
let vadEndAt = null;
let pendingSayQueue = [];
const speakingIds = new Set();
let mockTimer = null;
let mockIdx = 0;
let endTimer = null;
const END_TIMEOUT_MS = 3000;
let micGen = 0;
let micStream = null;
let chatGroupCount = 0;
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
  if (el.opControls) el.opControls.hidden = isRep;
  renderHero();
  renderSchedule();
  renderTerms();
  renderTranscript();
}

function inlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, '<code class="mono">$1</code>');
}

/** Light markdown → HTML for rep cards (headers, lists, tables, paragraphs). */
function renderMarkdown(md) {
  if (!md) return `<div class="hint">Select a scenario to load the playbook.</div>`;
  const lines = String(md).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  let para = [];

  const flushPara = () => {
    if (!para.length) return;
    out.push(`<p>${inlineMarkdown(para.join(" "))}</p>`);
    para = [];
  };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      flushPara();
      i += 1;
      continue;
    }

    const heading = /^(#{1,3})\s+(.+)$/.exec(trimmed);
    if (heading) {
      flushPara();
      const level = heading[1].length;
      out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (
      trimmed.includes("|") &&
      i + 1 < lines.length &&
      /^\|?\s*:?-{3,}/.test(lines[i + 1].trim())
    ) {
      flushPara();
      const rows = [];
      while (i < lines.length && lines[i].trim().includes("|")) {
        const cells = lines[i]
          .trim()
          .replace(/^\|/, "")
          .replace(/\|$/, "")
          .split("|")
          .map((c) => c.trim());
        if (!/^:?-{3,}/.test(cells[0] || "")) rows.push(cells);
        i += 1;
      }
      if (rows.length) {
        const head = rows[0];
        const body = rows.slice(1);
        out.push("<table>");
        out.push(
          `<thead><tr>${head.map((c) => `<th>${inlineMarkdown(c)}</th>`).join("")}</tr></thead>`,
        );
        out.push("<tbody>");
        for (const row of body) {
          out.push(
            `<tr>${row.map((c) => `<td>${inlineMarkdown(c)}</td>`).join("")}</tr>`,
          );
        }
        out.push("</tbody></table>");
      }
      continue;
    }

    const ol = /^(\d+)\.\s+(.+)$/.exec(trimmed);
    if (ol) {
      flushPara();
      out.push("<ol>");
      while (i < lines.length) {
        const m = /^(\d+)\.\s+(.+)$/.exec(lines[i].trim());
        if (!m) break;
        out.push(`<li>${inlineMarkdown(m[2])}</li>`);
        i += 1;
      }
      out.push("</ol>");
      continue;
    }

    const ul = /^[-*]\s+(.+)$/.exec(trimmed);
    if (ul) {
      flushPara();
      out.push("<ul>");
      while (i < lines.length) {
        const m = /^[-*]\s+(.+)$/.exec(lines[i].trim());
        if (!m) break;
        out.push(`<li>${inlineMarkdown(m[1])}</li>`);
        i += 1;
      }
      out.push("</ul>");
      continue;
    }

    para.push(trimmed);
    i += 1;
  }
  flushPara();
  return out.join("") || `<div class="hint">Empty playbook.</div>`;
}

function setRepCard(md) {
  store.set({ repCard: md || "" });
  if (el.repCard) el.repCard.innerHTML = renderMarkdown(md || "");
}

function updateCallButton() {
  const s = store.get();
  const active = s.started && !s.ending;
  for (const btn of [el.btnCall, el.btnCallOp]) {
    if (!btn) continue;
    btn.textContent = active ? "End chat" : "Start chat";
    btn.classList.toggle("danger", active);
    btn.disabled = Boolean(s.ending);
  }
}

function updateMicButton() {
  const on = store.get().micOn;
  el.btnMic.classList.toggle("active", on);
  el.btnMic.setAttribute("aria-label", on ? "Microphone on" : "Microphone off");
  el.btnMic.title = on ? "Mic on — click to stop" : "Mic off — click to talk";
}

function verdictState(s) {
  if (!s.started && !s.eval) return { key: "pending", label: "Pending" };
  if (s.phase === "ESCALATE") return { key: "infeasible", label: "Escalated" };
  if (s.phase === "END" && s.lastIntent === "NO_DEAL_WRAP") {
    return { key: "infeasible", label: "No deal" };
  }
  if (s.phase === "END" && s.lastIntent === "CLOSE") {
    return { key: "feasible", label: "Closed — pending client approval" };
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

function paymentSummary(ev) {
  if (!ev?.rows?.length) return null;
  const pays = ev.rows.filter((r) => (r.creditor_payment_cents || 0) > 0);
  if (!pays.length) return null;
  return {
    count: pays.length,
    firstDate: pays[0].date,
    lastDate: pays[pays.length - 1].date,
    amounts: pays.map((r) => r.creditor_payment_cents),
  };
}

function renderHero() {
  const s = store.get();
  const plain = PHASE_PLAIN[s.phase] || s.phase || "—";
  const intent = s.lastIntent || "—";
  const pay = paymentSummary(s.eval);

  el.heroMain.textContent = plain;
  el.heroMain.dataset.state =
    s.phase === "WRAP" || (s.phase === "END" && intent === "CLOSE")
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
  const subBits = [];
  if (intent && intent !== "—") subBits.push(`Intent ${intent}`);
  if (s.eval?.shape) subBits.push(`shape ${s.eval.shape}`);
  if (pay) subBits.push(`${pay.count} payments`);
  el.opHeroSub.textContent = subBits.length
    ? subBits.join(" · ")
    : s.phase
      ? `Phase ${s.phase}`
      : "No engine result yet";
  el.opHeroMetrics.innerHTML = `
    <div class="metric"><div class="k">Offer total</div><div class="v">${money(s.eval?.offer_total_cents)}</div></div>
    <div class="metric"><div class="k">Settlement</div><div class="v">${pct(s.eval?.agreed_bp)}</div></div>
    <div class="metric"><div class="k">Payments</div><div class="v">${pay ? pay.count : "—"}</div></div>
    <div class="metric"><div class="k">Max affordable <span class="badge private">PRIVATE</span></div><div class="v">${pct(s.eval?.max_bp)}</div></div>
  `;
  flash(el.heroMain);
  flash(el.opHeroMain);
}

function chatSide(role) {
  return role === "agent" ? "agent" : "rep";
}

function bubbleHtml(side, texts, extraClass = "", enter = false) {
  const who = side === "agent" ? "Agent" : "You";
  const body = texts.map((t) => `<p>${escapeHtml(t)}</p>`).join("");
  return (
    `<div class="msg ${side}${enter ? " enter" : ""}">` +
    `<div class="who">${who}</div>` +
    `<div class="bubble ${side}${extraClass}">${body}</div>` +
    `</div>`
  );
}

function renderChat() {
  const s = store.get();
  // The server streams one transcript line per sentence; group consecutive lines by speaker.
  const groups = [];
  for (const line of s.transcript) {
    const side = chatSide(line.role);
    const last = groups.at(-1);
    if (last && last.side === side) last.texts.push(line.text);
    else groups.push({ side, texts: [line.text] });
  }
  const parts = groups.map((g, i) => bubbleHtml(g.side, g.texts, "", i >= chatGroupCount));
  chatGroupCount = groups.length;
  if (s.interim) parts.push(bubbleHtml("rep", [s.interim], " interim"));
  if (s.waiting) parts.push(bubbleHtml("agent", ["Thinking…"], " typing"));
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
  const pay = paymentSummary(ev);
  const head = pay
    ? `<div class="hint" style="margin-bottom:8px">Offer ${money(ev.offer_total_cents)} at ${pct(ev.agreed_bp)} · ${pay.count} payment${pay.count === 1 ? "" : "s"} · ${escapeHtml(formatDate(pay.firstDate))} → ${escapeHtml(formatDate(pay.lastDate))}</div>`
    : `<div class="hint" style="margin-bottom:8px">Offer ${money(ev.offer_total_cents)} at ${pct(ev.agreed_bp)}</div>`;
  if (isRep) {
    return `
      ${head}
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
    ${head}
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
    case "term_alt":
      return `alt ${p.alt_field ?? "none"}=${p.alt_value ?? "none"} (fpd ${p.requested_fpd ?? ""})`;
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
  setRepCard(store.get().repCard);
  showNotice(store.get().sttFallbackNotice);
  updateCallButton();
  updateMicButton();
}

function resetCallState() {
  chatGroupCount = 0;
  pendingSayQueue = [];
  speakingIds.clear();
  window.speechSynthesis?.cancel();
  store.set({
    callId: null,
    connected: false,
    started: false,
    ending: false,
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
  el.btnDownload.disabled = !on || !store.get().callId;
  updateCallButton();
  updateMicButton();
}

function closeSocket() {
  clearTimeout(endTimer);
  endTimer = null;
  if (!ws) return;
  const old = ws;
  ws = null;
  old.onopen = old.onmessage = old.onclose = null;
  try {
    old.close();
  } catch {
    /* ignore */
  }
}

function connectAndStart() {
  closeSocket();
  resetCallState();
  const scenarioId = el.scenarioSelect.value || store.get().scenarioId || "";
  if (!scenarioId) {
    showNotice("Pick a scenario first (operator controls), or apply a custom test case.");
    updateCallButton();
    return;
  }
  const callId = crypto.randomUUID();
  store.set({ callId, connected: false, started: false, scenarioId });
  rememberScenarioId(scenarioId === CUSTOM_ID ? CUSTOM_ID : scenarioId);
  updateCallButton();
  const sock = new WebSocket(wsUrl(callId));
  ws = sock;
  sock.binaryType = "arraybuffer";
  // Handlers check identity so a late event from a replaced socket cannot clobber the new call.
  sock.onopen = () => {
    if (ws !== sock) return;
    store.set({ connected: true });
    const startMsg = { type: "start" };
    if (scenarioId === CUSTOM_ID) {
      const payload = store.get().customPayload;
      if (!payload) {
        showNotice("Custom case missing — open Operator and Apply custom case.");
        closeSocket();
        return;
      }
      startMsg.scenario_id = payload.meta?.id || "custom";
      startMsg.scenario_payload = payload;
    } else {
      startMsg.scenario_id = scenarioId;
    }
    sock.send(JSON.stringify(startMsg));
    store.set({ started: true });
    setControlsEnabled(true);
  };
  sock.onmessage = (ev) => {
    if (ws !== sock) return;
    try {
      applyEvent(JSON.parse(ev.data));
    } catch (err) {
      console.warn(err);
    }
  };
  sock.onclose = () => {
    if (ws !== sock) return;
    ws = null;
    clearTimeout(endTimer);
    endTimer = null;
    stopMic();
    store.set({ connected: false, waiting: false, started: false, ending: false });
    setControlsEnabled(false);
    updateCallButton();
  };
}

function sendText(textOverride, source) {
  const text = (textOverride ?? el.textInput.value).trim();
  if (!text) return;
  if (textOverride == null) el.textInput.value = "";
  if (store.get().mock) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  // Typed reply mid-TTS must barge first so confirm/counter bookkeeping lands.
  if (store.get().speaking || pendingSayQueue.length) bargeIn();
  // Show the rep bubble immediately; server echo replaces the pending line later.
  const transcript = [
    ...store.get().transcript,
    { role: "creditor", text, blocked: false, intent: null, pending: true },
  ];
  store.set({ waiting: true, interim: "", transcript });
  renderTranscript();
  const payload = { type: "text", text };
  if (source) payload.source = source;
  ws.send(JSON.stringify(payload));
}

function sendJson(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(obj));
}

function endChat() {
  if (store.get().mock) {
    stopMock();
    setControlsEnabled(false);
    updateCallButton();
    return;
  }
  if (store.get().ending) return;
  void stopMic();
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    store.set({ ending: true });
    finishEnd();
    return;
  }
  store.set({ ending: true, waiting: true });
  updateCallButton();
  sendJson({ type: "end" });
  renderChat();
  // Never leave the toggle stuck if the server never answers the end.
  clearTimeout(endTimer);
  endTimer = setTimeout(finishEnd, END_TIMEOUT_MS);
}

function finishEnd() {
  if (!store.get().ending) return;
  closeSocket();
  store.set({
    started: false,
    ending: false,
    waiting: false,
    connected: false,
    phase: "END",
  });
  setControlsEnabled(false);
  updateCallButton();
  renderHero();
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
    // F06: synthesis failure must still ack so server pending can commit.
    sendJson({ type: "sentence_done", id: next.id });
    const acked = [...store.get().ackedIds, next.id];
    store.set({ ackedIds: acked });
    speakingIds.delete(next.id);
    showNotice("Speech playback failed; continuing the turn.");
    drainSayQueue();
  };
  window.speechSynthesis.speak(utter);
}

function bargeIn() {
  if (!store.get().bargeInEnabled) return;
  if (!store.get().speaking && !pendingSayQueue.length) return;
  window.speechSynthesis.cancel();
  pendingSayQueue = [];
  // Include the in-flight sentence — ackedIds only updates on utter.onend.
  const spoken = [...new Set([...store.get().ackedIds, ...speakingIds])];
  store.set({ speaking: false });
  speakingIds.clear();
  sendJson({ type: "barge_in", spoken_ids: spoken });
}

function applyEvent(msg) {
  const type = msg.type;
  const s = store.get();

  if (type === "transcript") {
    let transcript = [...s.transcript];
    const role = msg.role === "agent" ? "agent" : "creditor";
    if (role === "creditor") {
      // Drop the optimistic pending bubble that matches this server echo.
      const idx = transcript.findIndex(
        (line) => line.pending && line.role === "creditor" && line.text === msg.text,
      );
      if (idx >= 0) transcript = transcript.filter((_, i) => i !== idx);
    }
    const line = {
      role,
      text: msg.text,
      blocked: Boolean(msg.blocked),
      intent: role === "agent" ? s.lastIntent : null,
    };
    store.set({ transcript: [...transcript, line], interim: "" });
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
        stopMic().then(() => startMic());
      }
    } else {
      showNotice(`STT error: ${msg.message || "unavailable"}`);
    }
    return;
  }
  if (type === "error") {
    store.set({ waiting: false });
    if (store.get().ending) {
      finishEnd();
      return;
    }
    showNotice(msg.message || "Error");
    renderChat();
    return;
  }
  if (type === "turn_done") {
    store.set({ waiting: false });
    renderChat();
    if (store.get().ending) {
      finishEnd();
    } else if (store.get().phase === "END") {
      stopMic();
      store.set({ started: false });
      setControlsEnabled(false);
      updateCallButton();
    }
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

function isMicPermissionError(err) {
  const name = err?.name || "";
  const msg = String(err?.message || err || "");
  return (
    name === "NotAllowedError" ||
    name === "NotFoundError" ||
    /Permission denied|not allowed|Requested device not found/i.test(msg)
  );
}

function isVadBackendError(err) {
  const msg = String(err?.message || err || "");
  return /no available backend|initializeWebAssembly|onnx|wasm|wasmpack|ort-/i.test(
    msg,
  );
}

function shortMicError(err) {
  if (isMicPermissionError(err)) {
    return "Browser blocked the mic. Click the lock/site icon in the address bar → allow Microphone, then try again.";
  }
  if (isVadBackendError(err)) {
    return "Voice-detect engine failed to load in this browser.";
  }
  const raw = String(err?.message || err || "unknown error");
  return raw.length > 160 ? `${raw.slice(0, 157)}…` : raw;
}

function fallBackToBrowserStt(reason) {
  store.set({
    sttMode: "browser",
    sttFallbackNotice: reason,
  });
  if (el.sttMode) el.sttMode.value = "browser";
  showNotice(reason);
  // Only start listening if the user still wants the mic on.
  if (store.get().micOn) {
    startBrowserRec();
  }
  updateMicButton();
}

async function resumeVadAudioContext(instance) {
  const ctx = instance?.audioContext;
  if (ctx && ctx.state === "suspended") {
    try {
      await ctx.resume();
    } catch {
      /* ignore */
    }
  }
}

async function startMic() {
  if (store.get().mock) return;
  // micOn flips immediately so a second click during VAD load routes to stopMic.
  const gen = ++micGen;
  store.set({ micOn: true, sttFallbackNotice: "" });
  updateMicButton();
  const mode = effectiveSttMode();
  if (mode === "browser") {
    startBrowserRec();
    return;
  }
  let instance = null;
  let stream = null;
  try {
    const { MicVAD } = await import(
      "https://cdn.jsdelivr.net/npm/@ricky0123/vad-web@0.0.22/+esm"
    );
    if (gen !== micGen) return;
    // vad-web@0.0.22 takes a ready ``stream`` (not ``getStream`` from newer docs).
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        autoGainControl: true,
        noiseSuppression: true,
      },
    });
    if (gen !== micGen) {
      stopTracks(stream);
      return;
    }
    micStream = stream;
    // Defaults (legacy): positiveSpeechThreshold=0.5 is too deaf for quiet mics.
    // frameSamples=1536 @ 16 kHz ≈ 96 ms/frame.
    instance = await MicVAD.new({
      stream,
      onnxWASMBasePath: onnxBase,
      baseAssetPath: vadAssetBase,
      positiveSpeechThreshold: 0.35,
      negativeSpeechThreshold: 0.2,
      // ~1.5 s of silence before cutting — mid-sentence pauses stay in one clip.
      redemptionFrames: 16,
      // ~1 s of audio before first speech-positive frame (catch word onsets).
      preSpeechPadFrames: 10,
      // ~290 ms minimum utterance; shorter → onVADMisfire.
      minSpeechFrames: 3,
      onSpeechStart: () => {
        if (gen !== micGen) return;
        if (store.get().speaking || pendingSayQueue.length) bargeIn();
      },
      onVADMisfire: () => {
        if (gen !== micGen) return;
        // Too short to send — keep listening; no toast spam.
      },
      onSpeechEnd: (audio) => {
        if (gen !== micGen) return;
        vadEndAt = performance.now();
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        ws.send(encodeWav(audio, 16000));
        store.set({ waiting: true });
        renderChat();
      },
    });
    // Async import often loses the user-gesture; AudioContext stays suspended
    // until a later click/TTS — mic appears "dead" for the first several turns.
    await resumeVadAudioContext(instance);
  } catch (err) {
    if (gen !== micGen) return;
    destroyVad(instance);
    stopTracks(stream);
    stream = null;
    micStream = null;
    // Auto/server: VAD is only the capture path — fall back to browser STT.
    if (
      !isMicPermissionError(err) &&
      (mode === "auto" || mode === "server" || isVadBackendError(err))
    ) {
      fallBackToBrowserStt(
        "Mic voice-detect failed to load — switched to browser speech recognition. Or just type.",
      );
      return;
    }
    showNotice(shortMicError(err));
    await stopMic();
    return;
  }
  if (gen !== micGen) {
    // Stopped while loading: tear down what was just created.
    destroyVad(instance);
    stopTracks(stream);
    micStream = null;
    return;
  }
  vad = instance;
  vad.start();
  await resumeVadAudioContext(instance);
}

function destroyVad(instance) {
  if (!instance) return;
  try {
    instance.pause();
  } catch {
    /* ignore */
  }
  try {
    // Prefer stopping tracks we own; also stop library stream if present.
    stopTracks(instance.stream);
  } catch {
    /* ignore */
  }
  try {
    instance.destroy();
  } catch {
    /* ignore */
  }
}

function stopTracks(stream) {
  stream?.getTracks?.().forEach((t) => {
    try {
      t.stop();
    } catch {
      /* ignore */
    }
  });
}

async function stopMic() {
  micGen += 1;
  store.set({ micOn: false, interim: "", sttFallbackNotice: "" });
  updateMicButton();
  const instance = vad;
  vad = null;
  destroyVad(instance);
  stopTracks(micStream);
  micStream = null;
  stopBrowserRec();
  showNotice("");
  renderChat();
}

function startBrowserRec() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    showNotice("Browser speech recognition is not available in this browser.");
    store.set({ micOn: false });
    updateMicButton();
    return;
  }
  stopBrowserRec();
  const rec = new SR();
  browserRec = rec;
  rec.continuous = true;
  rec.interimResults = true;
  rec.onstart = () => {
    browserActive = true;
  };
  rec.onspeechstart = () => {
    if (store.get().speaking || pendingSayQueue.length) bargeIn();
  };
  rec.onresult = (event) => {
    if (browserRec !== rec) return;
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
  rec.onerror = (ev) => {
    if (ev.error !== "no-speech" && ev.error !== "aborted") {
      showNotice(`Browser STT: ${ev.error}`);
    }
  };
  rec.onend = () => {
    browserActive = false;
    // Chrome ends continuous recognition on silence; restart only while still the live recognizer.
    if (browserRec === rec && store.get().micOn && effectiveSttMode() === "browser") {
      try {
        rec.start();
      } catch {
        /* ignore restart races */
      }
    }
  };
  rec.start();
  browserActive = true;
}

function stopBrowserRec() {
  if (browserRec) {
    const rec = browserRec;
    browserRec = null;
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
  browserActive = false;
}

function startMock() {
  resetCallState();
  store.set({ mock: true, started: true, connected: true });
  setControlsEnabled(false);
  el.btnMic.disabled = true;
  updateCallButton();
  mockIdx = 0;
  runMockStep();
}

function stopMock() {
  if (mockTimer) clearTimeout(mockTimer);
  mockTimer = null;
  mockIdx = 0;
  store.set({ mock: false, started: false, connected: false, ending: false });
  updateCallButton();
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
    const saved = store.get().scenarioId;
    const opts = [
      `<option value="">Select scenario…</option>`,
      ...list.map(
        (s) =>
          `<option value="${escapeAttr(s.id)}">${escapeHtml(s.title)}</option>`,
      ),
      `<option value="${CUSTOM_ID}">Custom test case</option>`,
    ];
    el.scenarioSelect.innerHTML = opts.join("");
    if (saved === CUSTOM_ID || (saved && list.some((s) => s.id === saved))) {
      el.scenarioSelect.value = saved;
    } else {
      el.scenarioSelect.value = "";
      store.set({ scenarioId: "" });
    }
    updateExpectedBadge();
    syncCustomCasePanel();
  } catch (err) {
    console.warn(err);
  }
  const id = el.scenarioSelect.value || store.get().scenarioId;
  if (id === CUSTOM_ID) {
    await restoreCustomCase();
  } else {
    await loadScenarioInfo(id);
  }
  syncCustomCasePanel();
}

function syncCustomCasePanel() {
  const show = (el.scenarioSelect.value || store.get().scenarioId) === CUSTOM_ID;
  if (el.customCaseShell) {
    el.customCaseShell.hidden = !show;
    el.customCaseShell.setAttribute("aria-hidden", show ? "false" : "true");
  } else if (el.customCasePanel) {
    el.customCasePanel.hidden = !show;
  }
}

function setCustomStatus(msg, ok = false) {
  if (!el.customStatus) return;
  el.customStatus.textContent = msg || "";
  el.customStatus.style.color = ok ? "var(--ok)" : "var(--muted)";
}

async function loadTemplateIntoEditor() {
  try {
    const res = await fetch("/scenarios/template");
    const tpl = await res.json();
    if (el.customJson) el.customJson.value = JSON.stringify(tpl, null, 2);
    setCustomStatus("Template loaded — edit, then Apply.");
    return tpl;
  } catch (err) {
    setCustomStatus(`Template fetch failed: ${err}`);
    return null;
  }
}

async function applyCustomCase() {
  if (!el.customJson) return;
  let payload;
  try {
    payload = JSON.parse(el.customJson.value);
  } catch (err) {
    setCustomStatus(`Invalid JSON: ${err.message || err}`);
    showNotice("Custom case JSON is invalid.");
    return;
  }
  try {
    const res = await fetch("/scenarios/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail || res.statusText;
      setCustomStatus(`Reject: ${detail}`);
      showNotice(`Custom case rejected: ${detail}`);
      return;
    }
    const brief = await res.json();
    store.set({ customPayload: payload, scenarioId: CUSTOM_ID });
    el.scenarioSelect.value = CUSTOM_ID;
    rememberScenarioId(CUSTOM_ID);
    try {
      localStorage.setItem(CUSTOM_KEY, JSON.stringify(payload));
    } catch {
      /* ignore */
    }
    renderScenarioBrief(brief);
    updateExpectedBadge();
    if (payload.rep_card) setRepCard(String(payload.rep_card));
    else setRepCard("");
    setCustomStatus(`Applied “${brief.title}”. Start chat to run it.`, true);
    syncCustomCasePanel();
    showNotice("");
  } catch (err) {
    setCustomStatus(`Apply failed: ${err}`);
  }
}

async function restoreCustomCase() {
  let payload = store.get().customPayload;
  if (!payload) {
    try {
      const raw = localStorage.getItem(CUSTOM_KEY);
      if (raw) payload = JSON.parse(raw);
    } catch {
      payload = null;
    }
  }
  if (!payload) {
    const tpl = await loadTemplateIntoEditor();
    if (tpl && el.customJson) {
      // Prefill + apply so brief/rep card aren't empty on first open.
      await applyCustomCase();
      setCustomStatus("Template applied — edit JSON and Apply to update.", true);
    } else {
      renderScenarioBrief(null);
      setRepCard("");
      setCustomStatus("No saved custom case — load template or paste JSON.");
    }
    return;
  }
  store.set({ customPayload: payload, scenarioId: CUSTOM_ID });
  if (el.customJson) el.customJson.value = JSON.stringify(payload, null, 2);
  try {
    const res = await fetch("/scenarios/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      const brief = await res.json();
      renderScenarioBrief(brief);
      updateExpectedBadge();
      if (payload.rep_card) setRepCard(String(payload.rep_card));
      setCustomStatus(`Restored “${brief.title}”.`, true);
    } else {
      renderScenarioBrief(null);
      setCustomStatus("Saved custom case failed preview — edit and Apply.");
    }
  } catch (err) {
    renderScenarioBrief(null);
    setCustomStatus(`Restore failed: ${err}`);
  }
}

async function loadScenarioInfo(id) {
  await Promise.all([loadRepCard(id), loadScenarioBrief(id)]);
}

function kvRow(label, value, isPrivate = false) {
  const badge = isPrivate ? ` <span class="badge private">PRIVATE</span>` : "";
  return `<div class="kv"><span class="k">${escapeHtml(label)}${badge}</span><span class="mono">${escapeHtml(value)}</span></div>`;
}

function ledgerTypeLabel(type) {
  if (type === "credit") return "SDA deposit";
  if (type === "debit") return "Scheduled debit";
  return type || "entry";
}

function renderUpcomingLedger(c) {
  const rows = Array.isArray(c.upcoming_ledger) ? c.upcoming_ledger : [];
  const nDep = c.upcoming_drafts || 0;
  const nDeb = rows.filter((e) => e.type === "debit").length;
  const summaryBits = [];
  if (nDep) {
    summaryBits.push(
      `${nDep} SDA deposit${nDep === 1 ? "" : "s"} totaling ${money(c.upcoming_deposits_cents)}`,
    );
  }
  if (nDeb || c.upcoming_withdrawals_cents) {
    summaryBits.push(
      `${nDeb} scheduled debit${nDeb === 1 ? "" : "s"} totaling ${money(c.upcoming_withdrawals_cents || 0)}`,
    );
  }
  const summary = summaryBits.length
    ? summaryBits.join("; ")
    : "None after as-of date";
  const list = rows.length
    ? `<ul class="brief-ledger">${rows
        .map(
          (e) =>
            `<li><span class="mono">${escapeHtml(formatDate(e.date))}</span>` +
            `<span>${escapeHtml(ledgerTypeLabel(e.type))}</span>` +
            `<span class="mono">${escapeHtml(money(e.amount_cents))}</span></li>`,
        )
        .join("")}</ul>`
    : "";
  return `
    ${kvRow("Committed cash after as-of", summary)}
    ${list}`;
}

function renderScenarioBrief(d) {
  if (!d) {
    el.briefTitle.textContent = "";
    el.scenarioBrief.innerHTML = `<div class="hint">Select a scenario to see its details.</div>`;
    return;
  }
  el.briefTitle.textContent = `${d.title} · expected ${d.expected}`;
  const c = d.client;
  const cr = d.creditor;
  const f = d.firm;
  el.scenarioBrief.innerHTML = `
    <div class="brief-grid">
      <div class="brief-card">
        <h3>Creditor</h3>
        ${kvRow("Name", cr.name)}
        ${kvRow("Creditor balance", money(cr.creditor_balance_cents))}
        ${kvRow("Original balance", money(cr.original_balance_cents))}
      </div>
      <div class="brief-card">
        <h3>Client <span class="badge private">PRIVATE</span></h3>
        ${kvRow("As of", formatDate(c.as_of_date))}
        ${kvRow("SDA balance now", money(c.sda_balance_cents))}
        ${kvRow("Recurring draft", `${money(c.draft_amount_cents)} on day ${c.draft_day} each month`)}
        ${kvRow("Draft window", `${formatDate(c.first_draft_date)} – ${formatDate(c.last_draft_date)}`)}
        ${renderUpcomingLedger(c)}
      </div>
      <div class="brief-card">
        <h3>Firm fees</h3>
        ${kvRow("Program fee rate", pct(f.program_fee_bp))}
        ${kvRow("Program fee", money(f.program_fee_cents))}
        ${kvRow("Bank fee / payment", money(f.bank_fee_cents))}
      </div>
    </div>`;
}

async function loadScenarioBrief(id) {
  if (!id) {
    renderScenarioBrief(null);
    return;
  }
  try {
    const res = await fetch(`/scenarios/${encodeURIComponent(id)}`);
    renderScenarioBrief(res.ok ? await res.json() : null);
  } catch {
    renderScenarioBrief(null);
  }
}

function updateExpectedBadge() {
  const id = el.scenarioSelect.value;
  if (id === CUSTOM_ID) {
    const meta = store.get().customPayload?.meta;
    el.expectedBadge.hidden = false;
    el.expectedBadge.textContent = meta?.expected || "custom";
    el.expectedBadge.title = meta?.description || "Custom test case";
    return;
  }
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
  if (!id) {
    setRepCard("");
    return;
  }
  try {
    const res = await fetch(`/scenarios/${encodeURIComponent(id)}/rep_card`);
    if (!res.ok) {
      setRepCard("");
      return;
    }
    const data = await res.json();
    setRepCard(data.markdown || "");
  } catch {
    setRepCard("");
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
async function toggleCall() {
  const s = store.get();
  if (s.ending) return;
  if (s.started) {
    endChat();
    return;
  }
  await stopMic();
  stopMock();
  showNotice("");
  if (el.togMock.checked) {
    startMock();
    return;
  }
  const scenarioId = el.scenarioSelect.value || store.get().scenarioId || "";
  if (!scenarioId) {
    showNotice("Pick a scenario in Operator view (or apply a custom test case) before starting.");
    return;
  }
  if (scenarioId === CUSTOM_ID && !store.get().customPayload) {
    showNotice("Apply a custom test case on the Operator page first.");
    return;
  }
  connectAndStart();
}

el.btnCall.addEventListener("click", () => {
  toggleCall();
});
if (el.btnCallOp) {
  el.btnCallOp.addEventListener("click", () => {
    toggleCall();
  });
}
el.btnSend.addEventListener("click", () => sendText());
el.textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendText();
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
  const id = el.scenarioSelect.value;
  store.set({ scenarioId: id });
  rememberScenarioId(id);
  updateExpectedBadge();
  syncCustomCasePanel();
  if (id === CUSTOM_ID) await restoreCustomCase();
  else await loadScenarioInfo(id);
});
el.btnDownload.addEventListener("click", downloadLog);
if (el.btnLoadTemplate) {
  el.btnLoadTemplate.addEventListener("click", () => loadTemplateIntoEditor());
}
if (el.btnApplyCustom) {
  el.btnApplyCustom.addEventListener("click", () => applyCustomCase());
}
if (el.customFile) {
  el.customFile.addEventListener("change", async () => {
    const file = el.customFile.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      JSON.parse(text);
      if (el.customJson) el.customJson.value = text;
      setCustomStatus(`Loaded ${file.name} — click Apply.`);
    } catch (err) {
      setCustomStatus(`File not valid JSON: ${err.message || err}`);
    }
    el.customFile.value = "";
  });
}
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
  rememberScenarioId(params.get("scenario"));
}

initSplitter();
applyView();
renderAll();
renderScenarioBrief(null);
syncCustomCasePanel();
loadScenarios().then(() => {
  syncCustomCasePanel();
});

if (el.togMock.checked) startMock();
