/**
 * Page frame: header (title, tagline, view and theme toggles), scenario
 * cards, and the grid (conversation | decision trace | state).
 *
 * The view toggle's labels are for people outside the industry: "Debt
 * negotiator" (lens `operator`) and "Creditor rep" (lens `creditor`); the
 * internal lens values and the WS `view` are unchanged. With no `trace` slot
 * (the Creditor rep view) the grid is two columns.
 *
 * Phase 36: no call-phase badge in the header (the turn cards name each move);
 * a visitor's custom test cases join the cards (marked "Custom", with edit and
 * remove), "Add a test case" sits next to the picker's heading, and the
 * editor (`editor` slot) opens under the cards. All copy is sentence case.
 *
 * Layout: 3 columns at ≥1280 px, 2 at ≥900 px (state drops below), stacked
 * below 900. Children are passed in as slots so data sources can change
 * without touching layout.
 */
import { Lock, Moon, Pencil, Play, Plus, RotateCcw, Sun, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import { cn } from "@/lib/cn";
import { expectedLabel } from "@/lib/labels";
import type { Lens } from "@/lib/lens";
import type { Theme } from "@/hooks/useTheme";
import type { ScenarioMeta } from "@/types/protocol";

export interface AppShellProps {
  scenarios: ScenarioMeta[];
  selected: string;
  onSelect: (id: string) => void;
  onWatch: () => void;
  watchLabel: string;
  watchDisabled?: boolean;
  /** Why "Watch a call" is off (shown as its tooltip and description). */
  watchDisabledReason?: string;
  playing: boolean;
  lens: Lens;
  onLens: (l: Lens) => void;
  theme: Theme;
  onTheme: () => void;
  notice?: ReactNode;
  /** Extra call controls next to "Watch a call" (start/end a live call, download). */
  actions?: ReactNode;
  /** Custom test cases: open the editor, edit or remove a card. Omit to hide the controls. */
  onAddCase?: () => void;
  onEditCase?: (id: string) => void;
  onRemoveCase?: (id: string) => void;
  /** The test-case editor, shown under the cards while open. */
  editor?: ReactNode;
  conversation: ReactNode;
  /** Omit (null) to drop the decision-trace column. */
  trace?: ReactNode;
  state: ReactNode;
}

export const VIEW_LABEL: Record<Lens, string> = {
  operator: "Debt negotiator",
  creditor: "Creditor rep",
};

/** Under the view toggle: what the selected view shows (candidates in docs/PROGRESS.md, Phase 36). */
export const VIEW_HINT: Record<Lens, string> = {
  operator: "The agent's side of the call: the client's money and the reason behind every move.",
  creditor: "What the creditor's representative sees on the call, and nothing more.",
};

/** Under the title (candidates in docs/PROGRESS.md, Phase 36). */
export const TAGLINE =
  "An AI voice agent negotiates a debt settlement for a client. Ordinary code decides every number and every move; the AI only understands the other side and puts the replies into words.";

export function AppShell(p: AppShellProps) {
  const watchReasonId = "watch-disabled-reason";
  return (
    <div className="mx-auto flex min-h-screen max-w-[1800px] flex-col gap-4 px-4 py-4 wide:px-6">
      <header className="flex flex-wrap items-start gap-x-8 gap-y-3">
        <div className="min-w-0 flex-[1_1_28rem]">
          <h1 className="text-xl font-semibold tracking-tight text-balance">Settlement call console</h1>
          <p className="mt-0.5 max-w-[78ch] text-sm text-pretty text-muted">{TAGLINE}</p>
        </div>
        {/* View toggle and theme on one row, the view hint under them. */}
        <div className="flex min-w-0 flex-col items-start gap-1.5 mid:items-end">
          <div className="flex items-center gap-2">
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
              {p.theme === "dark" ? <Sun aria-hidden className="h-4 w-4" /> : <Moon aria-hidden className="h-4 w-4" />}
            </Button>
          </div>
          <p className="text-xs text-pretty text-muted mid:text-right" data-testid="view-hint">
            {VIEW_HINT[p.lens]}
          </p>
        </div>
      </header>

      {p.lens === "creditor" && (
        <p className="flex items-start gap-2 rounded-lg bg-surface-2 px-3 py-2 text-sm text-muted">
          <Lock aria-hidden className="mt-0.5 h-4 w-4 shrink-0" />
          You are looking at the call as the creditor's representative. The client's finances, the firm's fees and the
          agent's reasoning stay private.
        </p>
      )}

      <section aria-label="Scenarios" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <h2 className="text-base font-semibold">Pick a scenario</h2>
          {p.onAddCase && (
            <Button size="sm" variant="ghost" onClick={p.onAddCase} className="-ml-1 text-accent">
              <Plus aria-hidden className="h-4 w-4" />
              Add a test case
            </Button>
          )}
          <div className="flex w-full flex-wrap items-center gap-2 mid:ml-auto mid:w-auto">
            {p.actions}
            <Button
              variant="primary"
              onClick={p.onWatch}
              disabled={p.watchDisabled}
              title={p.watchDisabled ? p.watchDisabledReason : undefined}
              aria-describedby={p.watchDisabled && p.watchDisabledReason ? watchReasonId : undefined}
            >
              {p.playing ? <RotateCcw aria-hidden className="h-4 w-4" /> : <Play aria-hidden className="h-4 w-4" />}
              {p.watchLabel}
            </Button>
            {p.watchDisabled && p.watchDisabledReason && (
              <span id={watchReasonId} className="sr-only">
                {p.watchDisabledReason}
              </span>
            )}
          </div>
        </div>
        <ul className="-mx-4 flex snap-x scroll-px-4 list-none gap-3 overflow-x-auto px-4 pb-1 wide:mx-0 wide:grid wide:grid-cols-[repeat(auto-fit,minmax(12rem,1fr))] wide:overflow-visible wide:px-0">
          {p.scenarios.map((s) => (
            <ScenarioCard
              key={s.id}
              scenario={s}
              selected={p.selected === s.id}
              onSelect={() => p.onSelect(s.id)}
              onEdit={s.custom && p.onEditCase ? () => p.onEditCase!(s.id) : undefined}
              onRemove={s.custom && p.onRemoveCase ? () => p.onRemoveCase!(s.id) : undefined}
            />
          ))}
        </ul>
        {p.editor}
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

function ScenarioCard({
  scenario: s,
  selected,
  onSelect,
  onEdit,
  onRemove,
}: {
  scenario: ScenarioMeta;
  selected: boolean;
  onSelect: () => void;
  onEdit?: () => void;
  onRemove?: () => void;
}) {
  const hasActions = Boolean(onEdit || onRemove);
  return (
    <li className="relative flex w-60 shrink-0 snap-start wide:w-auto">
      <button
        type="button"
        aria-pressed={selected}
        onClick={onSelect}
        className={cn(
          "flex w-full cursor-pointer flex-col gap-1 rounded-xl border bg-surface p-3 text-left transition-colors",
          selected ? "border-accent ring-1 ring-accent" : "border-border hover:border-faint",
        )}
      >
        {/* Only the title makes room for the edit / remove icons in the corner. */}
        <span className={cn("font-semibold break-words", hasActions && "pr-16")}>{s.title}</span>
        {s.description && <span className="line-clamp-3 text-sm text-muted">{s.description}</span>}
        <span className="mt-auto flex flex-wrap items-center gap-1.5 pt-2">
          {s.custom && <Badge tone="accent">Custom</Badge>}
          <span className="text-xs text-faint">Expected outcome</span>
          <Badge>{expectedLabel(s.expected)}</Badge>
        </span>
        {s.custom && <span className="text-xs text-muted">You play the rep on this case</span>}
      </button>
      {hasActions && (
        <span className="absolute top-2 right-2 flex gap-0.5">
          {onEdit && (
            <Button size="icon" variant="ghost" className="h-8 w-8" aria-label={`Edit ${s.title}`} onClick={onEdit}>
              <Pencil aria-hidden className="h-3.5 w-3.5" />
            </Button>
          )}
          {onRemove && (
            <Button size="icon" variant="ghost" className="h-8 w-8" aria-label={`Remove ${s.title}`} onClick={onRemove}>
              <Trash2 aria-hidden className="h-3.5 w-3.5" />
            </Button>
          )}
        </span>
      )}
    </li>
  );
}
