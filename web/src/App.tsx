/**
 * Top-level wiring: picks the event source, folds it into state, and connects
 * the conversation controls to the call socket and the voice engine.
 *
 * Live (default): scenario cards from `/scenarios`; the state column opens
 * with the operator's brief, or in the creditor's eye with "Your account"
 * (the rep's own balances and rules from `/scenarios/{id}/rep`). "Start call" opens
 * `/ws/call/{id}?view=…` and the visitor plays the creditor rep by typing,
 * clicking a suggested reply, or speaking; "Watch a call" starts the same
 * socket in autoplay (the server's sim creditor plays the rep). The socket's
 * view is fixed per call: switching to the creditor's eye mid-call re-filters
 * on the client, and switching back cannot restore what a rep stream never
 * carried, so App says so.
 *
 * `?fixture=1` replays `fixtures/call_easy_deal.json` with no backend
 * (`&speed=4` to speed up).
 */
import { Download, PhoneCall, PhoneOff } from "lucide-react";
import { lazy, type ReactNode, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { Conversation } from "@/components/Conversation";
import { ScenarioBrief } from "@/components/ScenarioBrief";
import { YourAccount } from "@/components/YourAccount";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { easyDeal, FIXTURE_SCENARIOS } from "@/fixtures";
import { useCall } from "@/hooks/useCall";
import { useFixtureReplay } from "@/hooks/useFixtureReplay";
import { useScenarioBrief, useScenarios } from "@/hooks/useScenarios";
import { useTheme } from "@/hooks/useTheme";
import { useVoice } from "@/hooks/useVoice";
import { type CallEvent, type CallState, foldCall, isCallOver } from "@/lib/callState";
import { type Lens, viewFor } from "@/lib/lens";
import { micEventFor, micReducer, type MicState } from "@/lib/mic";
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

/** The decision-trace and state slots for AppShell (lazy: they pull in Recharts). */
function columns(state: CallState, lens: Lens, brief?: ReactNode): { trace: ReactNode; state: ReactNode } {
  return {
    trace: (
      <Suspense fallback={<Loading />}>
        <DecisionTrace traces={state.traces} lens={lens} />
      </Suspense>
    ),
    state: (
      <div className="flex flex-col gap-4">
        {brief}
        <Suspense fallback={<Loading />}>
          <StatePanel state={state} lens={lens} />
        </Suspense>
      </div>
    ),
  };
}

function Notice({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-border bg-surface px-3 py-2 text-sm text-muted">{children}</p>;
}

// ---------------------------------------------------------------- fixture mode

function FixtureApp({ speed }: { speed: number }) {
  const [lens, setLens] = useState<Lens>("operator");
  const [theme, toggleTheme] = useTheme();
  const replay = useFixtureReplay(easyDeal.frames, speed);
  const events = useMemo(() => eventsForLens(replay.events, lens), [replay.events, lens]);
  const state = useMemo(() => foldCall(events), [events]);
  const scenario = FIXTURE_SCENARIOS[0]!;
  const repLines = state.messages.filter((m) => m.role === "creditor").length;

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
      phase={state.phase}
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
          nextSuggested={replay.playing ? repLines : undefined}
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
  deal: "Autoplay finished: deal drafted.",
  no_deal: "Autoplay finished: no deal.",
  escalate: "Autoplay finished: escalated to a human.",
  incomplete: "Autoplay stopped before the call finished.",
};

function LiveApp() {
  const [lens, setLens] = useState<Lens>("operator");
  const [theme, toggleTheme] = useTheme();
  const { scenarios, error: catalogError } = useScenarios(null);
  const [selected, setSelected] = useState("easy_deal");
  const call = useCall();

  const events = useMemo(() => eventsForLens(call.events, lens), [call.events, lens]);
  const state = useMemo(() => foldCall(events), [events]);
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
      engine.reset(autoplay);
      if (autoplay) void engine.stopMic();
      start(selected, { view: viewFor(lens), autoplay });
    },
    [start, engine, selected, lens],
  );

  const scenario = scenarios.find((s) => s.id === selected) ?? null;
  const brief = useScenarioBrief(selected, lens, scenarios.length > 0);
  const inCall = call.status === "connecting" || call.status === "live" || call.status === "ending";
  const repLines = state.messages.filter((m) => m.role === "creditor").length;
  // Operator: the private brief. Creditor's eye: the rep's own account and rules (rep-safe).
  const side =
    lens === "operator"
      ? brief && <ScenarioBrief brief={brief} lens={lens} />
      : scenarios.length > 0 && <YourAccount scenarioId={selected} />;
  const cols = columns(state, lens, side || null);

  const notices: string[] = [];
  if (catalogError) notices.push(catalogError);
  if (lens === "operator" && call.view === "rep" && call.events.length > 0) {
    notices.push("This call streams the creditor's view, so private detail is not available for it. Operator detail starts with the next call.");
  }
  if (state.autoplay) notices.push(OUTCOME[state.autoplay.outcome] ?? "Autoplay finished.");
  const lastError = state.errors.at(-1);
  if (lastError) notices.push(lastError);

  return (
    <AppShell
      scenarios={scenarios}
      selected={selected}
      onSelect={(id) => {
        // A finished call (autoplay done, END) leaves its socket open; it must not lock the picker.
        if (!inCall || over) setSelected(id);
      }}
      onWatch={() => startCall(true)}
      watchDisabled={!scenario || call.status === "connecting"}
      watchLabel={call.autoplay && call.events.length > 0 ? "Watch again" : "Watch a call"}
      playing={call.autoplay && call.events.length > 0}
      phase={state.phase}
      lens={lens}
      onLens={setLens}
      theme={theme}
      onTheme={toggleTheme}
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
          {inCall && !over ? (
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
      notice={notices.length > 0 ? <div className="flex flex-col gap-2">{notices.map((n) => <Notice key={n}>{n}</Notice>)}</div> : null}
      conversation={
        <Conversation
          messages={state.messages}
          mic={over ? "off" : voice.mic}
          suggested={scenario?.suggested ?? []}
          nextSuggested={live ? repLines : undefined}
          onSend={live ? (t) => send(t, scenario?.suggested.includes(t) ? "suggested" : "typed") : undefined}
          onMicToggle={live ? voice.toggleMic : undefined}
          emptyHint="Press “Start call” to play the creditor rep yourself (type, click a suggested reply, or use the mic), or “Watch a call” to let the simulated rep play it."
          idleHint={call.autoplay && inCall && !over ? "The simulated rep is playing this call" : "Start a call to reply as the rep"}
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
