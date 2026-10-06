/**
 * Page frame: header (title, call phase, lens and theme toggles), scenario
 * cards, and the three-column grid (conversation | decision trace | state).
 *
 * Layout: 3 columns at ≥1280 px, 2 at ≥900 px (state drops below), stacked
 * below 900. Children are passed in as slots so 23b can swap data sources
 * without touching layout.
 */
import { Lock, Moon, Play, RotateCcw, Sun } from "lucide-react";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import type { ScenarioCard } from "@/fixtures";
import { cn } from "@/lib/cn";
import type { Lens } from "@/lib/lens";
import type { Theme } from "@/hooks/useTheme";
import type { Phase } from "@/types/events";

export interface AppShellProps {
  scenarios: ScenarioCard[];
  selected: string;
  onSelect: (id: string) => void;
  onWatch: () => void;
  watchLabel: string;
  watchDisabled?: boolean;
  playing: boolean;
  phase: Phase | null;
  lens: Lens;
  onLens: (l: Lens) => void;
  theme: Theme;
  onTheme: () => void;
  notice?: ReactNode;
  conversation: ReactNode;
  trace: ReactNode;
  state: ReactNode;
}

const EXPECTED: Record<string, string> = {
  deal: "Expected: deal",
  counter: "Expected: counters, then deal",
  no_deal: "Expected: no deal",
  escalate: "Expected: escalate",
};

export function AppShell(p: AppShellProps) {
  return (
    <div className="mx-auto flex min-h-screen max-w-[1800px] flex-col gap-4 px-4 py-4 wide:px-6">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <div className="mr-auto min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">Settlement call console</h1>
          <p className="text-sm text-muted">
            An agent negotiates a debt settlement. Code decides every move; the LLM only reads the rep and phrases the reply.
          </p>
        </div>
        {p.phase && <Badge tone="accent" aria-label={`Call phase ${p.phase}`}>{p.phase}</Badge>}
        <Segmented<Lens>
          label="Lens"
          value={p.lens}
          onChange={p.onLens}
          options={[
            { value: "operator", label: "Operator" },
            { value: "creditor", label: "Creditor's eye" },
          ]}
        />
        <Button size="icon" variant="ghost" onClick={p.onTheme} aria-label={p.theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}>
          {p.theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </Button>
      </header>

      {p.lens === "creditor" && (
        <p className="flex items-center gap-2 rounded-lg bg-surface-2 px-3 py-2 text-sm text-muted">
          <Lock aria-hidden className="h-4 w-4" />
          Creditor's eye: only what the rep's stream carries. The client's finances, fees, and ceiling are locked.
        </p>
      )}

      <section aria-label="Scenarios" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-base font-semibold">Pick a scenario</h2>
          <Button variant="primary" onClick={p.onWatch} disabled={p.watchDisabled} className="ml-auto">
            {p.playing ? <RotateCcw className="h-4 w-4" /> : <Play className="h-4 w-4" />}
            {p.watchLabel}
          </Button>
        </div>
        <div className="-mx-4 flex snap-x gap-3 overflow-x-auto px-4 pb-1 wide:mx-0 wide:grid wide:grid-cols-6 wide:overflow-visible wide:px-0">
          {p.scenarios.map((s) => (
            <button
              key={s.id}
              type="button"
              aria-pressed={p.selected === s.id}
              onClick={() => p.onSelect(s.id)}
              className={cn(
                "flex w-60 shrink-0 snap-start cursor-pointer flex-col gap-1 rounded-xl border bg-surface p-3 text-left wide:w-auto",
                p.selected === s.id ? "border-accent ring-1 ring-accent" : "border-border hover:border-faint",
              )}
            >
              <span className="font-semibold">{s.title}</span>
              <span className="text-sm text-muted">{s.description}</span>
              <span className="mt-auto pt-1 text-xs text-faint">{EXPECTED[s.expected] ?? `Expected: ${s.expected}`}</span>
            </button>
          ))}
        </div>
        {p.notice}
      </section>

      <main className="grid flex-1 grid-cols-1 gap-4 mid:grid-cols-2 wide:grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)_minmax(0,1fr)]">
        <div className="min-w-0 mid:max-wide:sticky mid:max-wide:top-4 mid:self-start">{p.conversation}</div>
        <div className="min-w-0">{p.trace}</div>
        <div className="min-w-0 mid:col-span-2 wide:col-span-1">{p.state}</div>
      </main>
    </div>
  );
}
