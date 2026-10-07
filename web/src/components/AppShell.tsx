/**
 * Page frame: header (title, call phase, view and theme toggles), scenario
 * cards, and the grid (conversation | decision trace | state).
 *
 * The view toggle's labels are for people outside the industry: "Debt
 * negotiator" (lens `operator`) and "Creditor rep" (lens `creditor`); the
 * internal lens values and the WS `view` are unchanged. With no `trace` slot
 * (the Creditor rep view) the grid is two columns.
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
import { cn } from "@/lib/cn";
import type { Lens } from "@/lib/lens";
import type { Theme } from "@/hooks/useTheme";
import type { Phase, ScenarioMeta } from "@/types/protocol";

export interface AppShellProps {
  scenarios: ScenarioMeta[];
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
  /** Extra call controls next to "Watch a call" (start/end a live call, download). */
  actions?: ReactNode;
  conversation: ReactNode;
  /** Omit (null) to drop the decision-trace column. */
  trace?: ReactNode;
  state: ReactNode;
}

export const VIEW_LABEL: Record<Lens, string> = {
  operator: "Debt negotiator",
  creditor: "Creditor rep",
};

export const VIEW_HINT: Record<Lens, string> = {
  operator: "The agent's side: client money and why each move was made.",
  creditor: "What the creditor's representative sees on the call.",
};

const EXPECTED: Record<string, string> = {
  deal: "Expected: deal",
  counter: "Expected: counters, then deal",
  no_deal: "Expected: no deal",
  escalate: "Expected: escalate",
};

export function AppShell(p: AppShellProps) {
  return (
    <div className="mx-auto flex min-h-screen max-w-[1800px] flex-col gap-4 px-4 py-4 wide:px-6">
      <header className="flex flex-wrap items-start gap-x-6 gap-y-3">
        <div className="mr-auto min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">Settlement call console</h1>
          <p className="text-sm text-muted">
            An agent negotiates a debt settlement. Code decides every move; the LLM only reads the rep and phrases the reply.
          </p>
        </div>
        {/* One row of controls (phase, view toggle, theme) with the view hint
            right-aligned under the whole row, so nothing floats between lines. */}
        <div className="flex flex-col items-start gap-1.5 mid:items-end">
          <div className="flex items-center gap-3">
            {p.phase && (
              <Badge tone="accent" aria-label={`Call phase ${p.phase}`}>
                {p.phase}
              </Badge>
            )}
            <Segmented<Lens>
              label="View"
              value={p.lens}
              onChange={p.onLens}
              options={[
                { value: "operator", label: VIEW_LABEL.operator },
                { value: "creditor", label: VIEW_LABEL.creditor },
              ]}
            />
            <Button size="icon" variant="ghost" onClick={p.onTheme} aria-label={p.theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}>
              {p.theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
          </div>
          <p className="text-xs text-muted mid:text-right" data-testid="view-hint">
            {VIEW_HINT[p.lens]}
          </p>
        </div>
      </header>

      {p.lens === "creditor" && (
        <p className="flex items-center gap-2 rounded-lg bg-surface-2 px-3 py-2 text-sm text-muted">
          <Lock aria-hidden className="h-4 w-4" />
          Creditor rep view: only what the creditor's representative would see. The client's finances, the firm's fees, and the agent's reasoning stay private.
        </p>
      )}

      <section aria-label="Scenarios" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="text-base font-semibold">Pick a scenario</h2>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {p.actions}
            <Button variant="primary" onClick={p.onWatch} disabled={p.watchDisabled}>
              {p.playing ? <RotateCcw className="h-4 w-4" /> : <Play className="h-4 w-4" />}
              {p.watchLabel}
            </Button>
          </div>
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

      {p.trace ? (
        <main className="grid flex-1 grid-cols-1 gap-4 mid:grid-cols-2 wide:grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)_minmax(0,1fr)]">
          <div className="min-w-0 mid:max-wide:sticky mid:max-wide:top-4 mid:self-start">{p.conversation}</div>
          <div className="min-w-0">{p.trace}</div>
          <div className="min-w-0 mid:col-span-2 wide:col-span-1">{p.state}</div>
        </main>
      ) : (
        <main className="grid flex-1 grid-cols-1 gap-4 mid:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
          <div className="min-w-0 mid:sticky mid:top-4 mid:self-start">{p.conversation}</div>
          <div className="min-w-0">{p.state}</div>
        </main>
      )}
    </div>
  );
}
