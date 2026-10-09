/**
 * Top-level wiring: picks the event source, folds it into state, and connects
 * the conversation controls to the call socket and the voice engine.
 *
 * Two views (user-facing names; the lens values stay `operator` / `creditor`):
 * "Debt negotiator" shows the decision trace, the operator brief and the
 * client's ledger; "Creditor rep" shows only the conversation, "Your account"
 * (the rep's own balances and rules) and the agreed terms. Live (default):
 * scenario cards from `/scenarios`. "Start call" opens `/ws/call/{id}?view=…`
 * and the visitor plays the creditor rep by typing, clicking a suggested
 * reply, or speaking; "Watch a call" starts the same socket in autoplay (the
 * server's sim creditor plays the rep).
 *
 * The socket's view is fixed per call. Switching to the Creditor rep view
 * mid-call re-filters on the client. Switching to the Debt negotiator view
 * during or after a call that streams the rep view backfills the decision
 * trace, ladder, latency and audit from `GET /calls/{id}/operator` and
 * refetches on every `turn_done` (Phase 36), so the trace is there whichever
 * view the call was started in.
 *
 * Custom test cases (Phase 36): "Add a test case" opens `CaseEditor`; saved
 * cases join the cards, persist in `localStorage` and start with
 * `start.scenario_payload` (played by hand: autoplay is curated-only).
 *
 * `?fixture=1` replays `fixtures/call_easy_deal.json` with no backend
 * (`&speed=4` to speed up).
 *
 * Phase 48: the selected scenario and view are in the URL
 * (`?scenario=&view=operator|rep`, `useUrlState`; back and forward work), a
 * handoff is shown as the call's outcome in both views (`lib/outcome.ts`),
 * and the transcript ends with a line saying how the call ended.
 */
import { Download, PhoneCall, PhoneOff } from "lucide-react";
import { lazy, type ReactNode, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { CaseEditor } from "@/components/CaseEditor";
import { ClientLedger } from "@/components/ClientLedger";
import { Conversation } from "@/components/Conversation";
import { ScenarioBrief } from "@/components/ScenarioBrief";
import { YourAccount } from "@/components/YourAccount";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { easyDeal, FIXTURE_SCENARIOS } from "@/fixtures";
import { useCall } from "@/hooks/useCall";
import { useFixtureReplay } from "@/hooks/useFixtureReplay";
import { useOperatorDetail } from "@/hooks/useOperatorDetail";
import { useScenarioBrief, useScenarios, useScenarioTemplate } from "@/hooks/useScenarios";
import { useTheme } from "@/hooks/useTheme";
import { readUrlState, useUrlState } from "@/hooks/useUrlState";
import { useVoice } from "@/hooks/useVoice";
import { type CallEvent, type CallState, foldCall, isCallOver, withOperatorDetail } from "@/lib/callState";
import {
  type CustomCase,
  customMeta,
  isCustomId,
  loadCases,
  metaOf,
  nextKey,
  saveCases,
  type ScenarioSource,
  serverId,
} from "@/lib/customCases";
import { type Lens, viewFor } from "@/lib/lens";
import { micEventFor, micReducer, type MicState } from "@/lib/mic";
import { endedLine } from "@/lib/outcome";
import { toRepView } from "@/lib/repView";

// Recharts lives in these two; loading them after first paint keeps the shell fast.
const DecisionTrace = lazy(() => import("@/components/DecisionTrace").then((m) => ({ default: m.DecisionTrace })));
const StatePanel = lazy(() => import("@/components/StatePanel").then((m) => ({ default: m.StatePanel })));

function params(): { fixture: boolean; speed: number } {
  const q = new URLSearchParams(window.location.search);
  const speed = Number(q.get("speed") ?? "1");
  return { fixture: q.get("fixture") === "1", speed: Number.isFinite(speed) && speed > 0 ? speed : 1 };
}

/** Filter raw events through the lens, as the server would per view (idempotent on rep data). */
export function eventsForLens(events: readonly CallEvent[], lens: Lens): CallEvent[] {
  if (lens === "operator") return [...events];
  return events.map(toRepView).filter((e): e is CallEvent => e !== null);
}

export function micFromEvents(events: readonly CallEvent[]): MicState {
  // Replay assumes the mic was switched on when the call started.
  let mic: MicState = events.length > 0 ? "listening" : "off";
  for (const ev of events) {
    if (ev.type === "tts_onset") continue;
    const m = micEventFor(ev);
    if (m) mic = micReducer(mic, m);
  }
  return mic;
}

const Loading = () => <Card className="h-48 animate-pulse" aria-hidden />;

export default function App() {
  const { fixture, speed } = useMemo(() => params(), []);
  return fixture ? <FixtureApp speed={speed} /> : <LiveApp />;
}

/** Trace column note when the server no longer holds a rep-view call's operator detail. */
export const TRACE_GONE_NOTE =
  "The server no longer has this call's decision trace (it keeps recent calls in memory only). Start a new call to see each move.";

/** Why "Watch a call" is off for a custom case. */
export const CUSTOM_WATCH_REASON =
  "The simulated rep only knows the built-in cases. Press Start call to play the rep yourself.";

/**
 * The decision-trace and state slots for AppShell (lazy: they pull in Recharts).
 * The Creditor rep view has no trace column. `context` (brief and ledger, or
 * "Your account") sits under the agreement and schedule.
 */
function columns(
  state: CallState,
  lens: Lens,
  context?: ReactNode,
  traceNote?: string,
): { trace: ReactNode; state: ReactNode } {
  return {
    trace:
      lens === "operator" ? (
        <Suspense fallback={<Loading />}>
          <DecisionTrace traces={state.traces} lens={lens} note={traceNote} />
        </Suspense>
      ) : null,
    state: (
      <Suspense fallback={<Loading />}>
        <StatePanel state={state} lens={lens} context={context} />
      </Suspense>
    ),
  };
}

function Notice({ children }: { children: ReactNode }) {
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border bg-surface px-3 py-2 text-sm text-muted">
      {children}
    </div>
  );
}

// ---------------------------------------------------------------- fixture mode

/** Fixture mode has one recorded call, so a scenario in the URL is ignored. */
const refuseScenario = () => false;

function FixtureApp({ speed }: { speed: number }) {
  const [lens, setLens] = useState<Lens>(() => readUrlState().lens ?? "operator");
  useUrlState({ scenario: null, lens, onScenario: refuseScenario, onLens: setLens });
  const [theme, toggleTheme] = useTheme();
  const replay = useFixtureReplay(easyDeal.frames, speed);
  const events = useMemo(() => eventsForLens(replay.events, lens), [replay.events, lens]);
  const state = useMemo(() => foldCall(events), [events]);
  const scenario = FIXTURE_SCENARIOS[0]!;

  const { start } = replay;
  useEffect(() => start(), [start]);
  const cols = columns(state, lens);

  return (
    <AppShell
      scenarios={FIXTURE_SCENARIOS}
      selected={scenario.id}
      onSelect={() => {}}
      onWatch={replay.start}
      watchLabel={replay.playing || replay.done ? "Replay the call" : "Watch a call"}
      playing={replay.playing || replay.done}
      lens={lens}
      onLens={setLens}
      theme={theme}
      onTheme={toggleTheme}
      notice={<Notice>Fixture mode: replaying a recorded synthetic call. No backend is used.</Notice>}
      conversation={
        <Conversation
          messages={state.messages}
          mic={isCallOver(state) ? "off" : micFromEvents(events)}
          suggested={scenario.suggested}
          ended={endedLine(state, lens)}
          emptyHint="Press “Watch a call” to replay a full negotiation."
        />
      }
      trace={cols.trace}
      state={cols.state}
    />
  );
}

// ---------------------------------------------------------------- live mode

const OUTCOME: Record<string, string> = {
  deal: "The simulated call ended with a deal drafted.",
  no_deal: "The simulated call ended with no deal.",
  escalate: "The simulated call was handed off to a specialist.",
  incomplete: "The simulated call stopped before it finished.",
};

type EditorState = { mode: "add" } | { mode: "edit"; key: string };

const DEFAULT_SCENARIO = "easy_deal";

function LiveApp() {
  const [lens, setLens] = useState<Lens>(() => readUrlState().lens ?? "operator");
  const [theme, toggleTheme] = useTheme();
  const { scenarios: catalog, error: catalogError } = useScenarios(null);
  const [selected, setSelected] = useState(() => readUrlState().scenario ?? DEFAULT_SCENARIO);
  const call = useCall();

  // ------------------------------------------------ custom test cases
  const [cases, setCases] = useState<CustomCase[]>(() => loadCases());
  const [storageOk, setStorageOk] = useState(true);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [removed, setRemoved] = useState<{ item: CustomCase; index: number } | null>(null);
  const template = useScenarioTemplate(editor !== null);
  const updateCases = (next: CustomCase[]) => {
    setCases(next);
    setStorageOk(saveCases(next));
  };
  const scenarios = useMemo(() => [...cases.map(customMeta), ...catalog], [cases, catalog]);
  const custom = isCustomId(selected) ? (cases.find((c) => c.key === selected) ?? null) : null;
  const source = useMemo<ScenarioSource | null>(
    () =>
      custom
        ? { kind: "custom", key: custom.key, version: custom.version, payload: custom.payload }
        : catalog.some((s) => s.id === selected)
          ? { kind: "catalog", id: selected }
          : null,
    [custom, catalog, selected],
  );

  // ------------------------------------------------ call state (+ operator backfill)
  const repStream = call.view === "rep" && call.lastCallId != null && call.events.length > 0;
  const backfillId = lens === "operator" && repStream ? call.lastCallId : null;
  const tick = useMemo(() => call.events.filter((e) => e.type === "turn_done" || e.type === "autoplay_done").length, [call.events]);
  const detail = useOperatorDetail(backfillId, tick);
  const events = useMemo(() => eventsForLens(call.events, lens), [call.events, lens]);
  const state = useMemo(() => {
    const folded = foldCall(events);
    return detail.status === "ready" ? withOperatorDetail(folded, detail.detail) : folded;
  }, [events, detail]);
  const stateRef = useRef(state);
  useLayoutEffect(() => {
    stateRef.current = state;
  });

  const live = call.status === "live" && !call.autoplay && !isCallOver(state);
  const voice = useVoice({
    sendJson: call.sendJson,
    sendWav: call.sendWav,
    sendRepText: (text, source) => send(text, source),
    currentTurn: () => stateRef.current.turn || null,
    onTtsOnset: (turn, ms) => call.addLocal({ type: "tts_onset", turn, ms }),
  });
  const { engine } = voice;

  const send = (text: string, source = "typed") => {
    if (!live) return;
    engine.beforeRepText();
    if (!call.sendText(text, source)) engine.onServerEvent({ type: "turn_done" });
  };

  const { subscribe } = call;
  useEffect(() => subscribe((ev) => engine.onServerEvent(ev)), [subscribe, engine]);

  // The mic goes off when the call is over or the socket is gone.
  const over = isCallOver(state) || call.status === "closed" || state.autoplay !== null;
  useEffect(() => {
    if (over) void engine.stopMic(true);
  }, [over, engine]);

  const { start } = call;
  const startCall = useCallback(
    (autoplay: boolean) => {
      if (custom && autoplay) return;
      engine.reset(autoplay);
      if (autoplay) void engine.stopMic();
      if (custom) start(serverId(custom.payload), { view: viewFor(lens), payload: custom.payload });
      else start(selected, { view: viewFor(lens), autoplay });
    },
    [start, engine, selected, lens, custom],
  );

  const scenario = scenarios.find((s) => s.id === selected) ?? null;
  const brief = useScenarioBrief(source, lens, source !== null);
  const inCall = call.status === "connecting" || call.status === "live" || call.status === "ending";
  const callRunning = inCall && !over;

  // A link or back/forward may name a scenario; the picker stays locked while a call runs.
  const pickLocked = useRef(false);
  useLayoutEffect(() => {
    pickLocked.current = inCall && !over;
  });
  const pickFromUrl = useCallback((id: string) => {
    if (pickLocked.current) return false;
    setSelected(id);
    return true;
  }, []);
  useUrlState({ scenario: selected, lens, onScenario: pickFromUrl, onLens: setLens });
  // A link to a scenario that does not exist (or a test case this browser lacks) falls back to the
  // default once the catalog is in (adjusting state while rendering, as React documents).
  if (catalog.length > 0 && scenario === null) {
    setSelected(catalog.some((s) => s.id === DEFAULT_SCENARIO) ? DEFAULT_SCENARIO : catalog[0]!.id);
  }
  // Debt negotiator: the private brief and client ledger. Creditor rep: the rep's own account and rules (rep-safe).
  const side =
    lens === "operator"
      ? brief && (
          <>
            <ScenarioBrief brief={brief} lens={lens} />
            <ClientLedger brief={brief} lens={lens} />
          </>
        )
      : source && <YourAccount source={source} />;
  const traceNote = backfillId && detail.status === "gone" ? TRACE_GONE_NOTE : undefined;
  const cols = columns(state, lens, side || null, traceNote);

  const saveCase = (payload: Record<string, unknown>, suggested: string[]) => {
    const meta = metaOf(payload);
    if (editor?.mode === "edit") {
      updateCases(cases.map((c) => (c.key === editor.key ? { ...c, ...meta, payload, suggested, version: c.version + 1 } : c)));
      setSelected(editor.key);
    } else {
      const key = nextKey(cases);
      updateCases([{ key, payload, suggested, version: 1, ...meta }, ...cases]);
      if (!callRunning) setSelected(key);
    }
    setRemoved(null);
    setEditor(null);
  };
  const removeCase = (key: string) => {
    const index = cases.findIndex((c) => c.key === key);
    if (index < 0) return;
    updateCases(cases.filter((c) => c.key !== key));
    setRemoved({ item: cases[index]!, index });
    if (editor?.mode === "edit" && editor.key === key) setEditor(null);
    if (selected === key) setSelected(catalog[0]?.id ?? DEFAULT_SCENARIO);
  };
  const undoRemove = () => {
    if (!removed) return;
    const next = [...cases];
    next.splice(Math.min(removed.index, next.length), 0, removed.item);
    updateCases(next);
    setRemoved(null);
  };
  const editing = editor?.mode === "edit" ? cases.find((c) => c.key === editor.key) : undefined;
  const editorText =
    editor?.mode === "edit" ? (editing ? JSON.stringify(editing.payload, null, 2) : null) : template;

  const notices: { key: string; body: ReactNode }[] = [];
  const note = (key: string, body: ReactNode) => notices.push({ key, body });
  if (catalogError) note("catalog", catalogError);
  if (removed) {
    note(
      "removed",
      <>
        <span>Removed “{removed.item.title}”.</span>
        <Button size="sm" variant="ghost" className="text-accent" onClick={undoRemove}>
          Undo
        </Button>
      </>,
    );
  }
  if (!storageOk) note("storage", "This browser is not saving custom test cases, so they will be gone after a reload.");
  if (custom && !callRunning) note("custom", "This is your own test case. The simulated rep only knows the built-in cases, so press Start call and play the rep yourself.");
  if (state.autoplay) note("outcome", OUTCOME[state.autoplay.outcome] ?? "The simulated call finished.");
  const lastError = state.errors.at(-1);
  if (lastError) note("error", lastError);

  return (
    <AppShell
      scenarios={scenarios}
      selected={selected}
      onSelect={(id) => {
        // A finished call (autoplay done, END) leaves its socket open; it must not lock the picker.
        if (!inCall || over) setSelected(id);
      }}
      onWatch={() => startCall(true)}
      watchDisabled={!scenario || call.status === "connecting" || custom !== null}
      watchDisabledReason={custom ? CUSTOM_WATCH_REASON : undefined}
      watchLabel={call.autoplay && call.events.length > 0 ? "Watch again" : "Watch a call"}
      playing={call.autoplay && call.events.length > 0}
      lens={lens}
      onLens={setLens}
      theme={theme}
      onTheme={toggleTheme}
      onAddCase={() => setEditor({ mode: "add" })}
      onEditCase={callRunning ? undefined : (id) => setEditor({ mode: "edit", key: id })}
      onRemoveCase={callRunning ? undefined : removeCase}
      editor={
        editor &&
        (editorText == null ? (
          <Loading />
        ) : (
          <CaseEditor
            key={editor.mode === "edit" ? `${editor.key}@${editing?.version}` : "add"}
            mode={editor.mode}
            initialText={editorText}
            templateText={template}
            onSave={saveCase}
            onCancel={() => setEditor(null)}
          />
        ))
      }
      actions={
        <>
          {call.lastCallId && (
            <a
              className="inline-flex h-10 items-center gap-2 rounded-lg border border-border bg-surface px-4 text-sm font-medium hover:bg-surface-2"
              href={`/calls/${encodeURIComponent(call.lastCallId)}/export?view=${viewFor(lens)}`}
              download={`call-${call.lastCallId}.json`}
            >
              <Download className="h-4 w-4" aria-hidden /> Download log
            </a>
          )}
          {callRunning ? (
            <Button onClick={call.end} disabled={call.status === "ending"}>
              <PhoneOff className="h-4 w-4" aria-hidden /> End call
            </Button>
          ) : (
            <Button onClick={() => startCall(false)} disabled={!scenario}>
              <PhoneCall className="h-4 w-4" aria-hidden /> Start call
            </Button>
          )}
        </>
      }
      notice={
        notices.length > 0 ? (
          <div className="flex flex-col gap-2" aria-live="polite">
            {notices.map((n) => (
              <Notice key={n.key}>{n.body}</Notice>
            ))}
          </div>
        ) : null
      }
      conversation={
        <Conversation
          messages={state.messages}
          mic={over ? "off" : voice.mic}
          suggested={scenario?.suggested ?? []}
          ended={endedLine(state, lens)}
          onSend={live ? (t) => send(t, scenario?.suggested.includes(t) ? "suggested" : "typed") : undefined}
          onMicToggle={live ? voice.toggleMic : undefined}
          emptyHint={
            custom
              ? "Press “Start call” to play the creditor rep on your test case: type, click a suggested reply, or use the mic."
              : "Press “Start call” to play the creditor rep yourself (type, click a suggested reply, or use the mic), or “Watch a call” to let the simulated rep play it."
          }
          idleHint={call.autoplay && callRunning ? "The simulated rep is playing this call" : "Start a call to reply as the rep"}
          interim={voice.snapshot.interim}
          notice={voice.snapshot.notice}
          onDismissNotice={() => engine.dismissNotice()}
          sttMode={voice.snapshot.sttMode}
          onSttMode={voice.setSttMode}
        />
      }
      trace={cols.trace}
      state={cols.state}
    />
  );
}
