/**
 * Top-level wiring for 23a: picks the event source and folds it into state.
 *
 * `?fixture=1` replays `fixtures/call_easy_deal.json` with its recorded
 * timing (`&speed=4` to speed up). Without it there is no backend wiring yet
 * (23b adds `useCall` over `/ws/call/{id}?view=…`), so the page shows the
 * empty console and a pointer to fixture mode.
 */
import { useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { Conversation } from "@/components/Conversation";
import { DecisionTrace } from "@/components/DecisionTrace";
import { StatePanel } from "@/components/StatePanel";
import { easyDeal, SCENARIOS } from "@/fixtures";
import { useFixtureReplay } from "@/hooks/useFixtureReplay";
import { useTheme } from "@/hooks/useTheme";
import { foldCall, isCallOver } from "@/lib/callState";
import type { Lens } from "@/lib/lens";
import { micEventFor, micReducer, type MicState } from "@/lib/mic";
import { toRepView } from "@/lib/repView";
import type { ServerEvent } from "@/types/events";

function params(): { fixture: boolean; speed: number } {
  const q = new URLSearchParams(window.location.search);
  const speed = Number(q.get("speed") ?? "1");
  return { fixture: q.get("fixture") === "1", speed: Number.isFinite(speed) && speed > 0 ? speed : 1 };
}

/** Filter raw operator events through the lens, as the server would per view. */
export function eventsForLens(events: readonly ServerEvent[], lens: Lens): ServerEvent[] {
  if (lens === "operator") return [...events];
  return events.map(toRepView).filter((e): e is ServerEvent => e !== null);
}

export function micFromEvents(events: readonly ServerEvent[]): MicState {
  // Replay assumes the mic was switched on when the call started.
  let mic: MicState = events.length > 0 ? "listening" : "off";
  for (const ev of events) {
    const m = micEventFor(ev);
    if (m) mic = micReducer(mic, m);
  }
  return mic;
}

export default function App() {
  const { fixture, speed } = useMemo(() => params(), []);
  const [lens, setLens] = useState<Lens>("operator");
  const [theme, toggleTheme] = useTheme();
  const [selected, setSelected] = useState("easy_deal");
  const replay = useFixtureReplay(easyDeal.frames, speed);

  const events = useMemo(() => eventsForLens(replay.events, lens), [replay.events, lens]);
  const state = useMemo(() => foldCall(events), [events]);
  const mic = micFromEvents(events);
  const scenario = SCENARIOS.find((s) => s.id === selected) ?? SCENARIOS[0]!;
  const repLines = state.messages.filter((m) => m.role === "creditor").length;
  const canReplay = fixture && selected === easyDeal.scenario_id;

  // Fixture mode plays the recording on load so the page shows a call at once.
  const { start } = replay;
  useEffect(() => {
    if (fixture) start();
  }, [fixture, start]);

  return (
    <AppShell
      scenarios={SCENARIOS}
      selected={selected}
      onSelect={setSelected}
      onWatch={replay.start}
      watchDisabled={!canReplay}
      watchLabel={replay.playing || replay.done ? "Replay the call" : "Watch a call"}
      playing={replay.playing || replay.done}
      phase={state.phase}
      lens={lens}
      onLens={setLens}
      theme={theme}
      onTheme={toggleTheme}
      notice={
        !fixture ? (
          <p className="rounded-lg border border-border bg-surface px-3 py-2 text-sm text-muted">
            Live calls are not wired in this build yet. Open{" "}
            <a className="text-accent underline" href="?fixture=1">
              fixture mode
            </a>{" "}
            to replay a recorded Easy deal call.
          </p>
        ) : !canReplay ? (
          <p className="rounded-lg border border-border bg-surface px-3 py-2 text-sm text-muted">
            Fixture mode has a recording for Easy deal only.
          </p>
        ) : null
      }
      conversation={
        <Conversation
          messages={state.messages}
          mic={isCallOver(state) ? "off" : mic}
          suggested={scenario.suggested}
          nextSuggested={replay.playing ? repLines : undefined}
          emptyHint={
            canReplay
              ? "Press “Watch a call” to replay a full negotiation. You'll see each rep line, the agent's reply, and why it said it."
              : "Pick a scenario to begin."
          }
        />
      }
      trace={<DecisionTrace traces={state.traces} lens={lens} />}
      state={<StatePanel state={state} lens={lens} />}
    />
  );
}
