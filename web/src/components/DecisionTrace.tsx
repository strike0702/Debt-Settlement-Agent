/**
 * Center column, "How the agent decided": one row per agent turn, newest
 * first. A collapsed row gives the move in plain words ("Countered at 31%"),
 * its key number, and the reason in a few words (`decide.reason_short`). An
 * open row tells the turn as a short story — What they said / What we heard /
 * Can the client pay? / Decision / What we said — with the technical detail
 * (affordability curve, what the agent now knows, the reply template, the
 * safety checks, the policy code) one more click away. The newest turn is
 * open until the visitor opens or closes a row themselves.
 *
 * Reads `turn_trace` events only; every sentence comes from
 * `lib/traceStory.ts` and the server's reason text. Debt negotiator view only:
 * the rep stream carries no traces (for a call streamed in the Creditor rep
 * view, App backfills them from `GET /calls/{id}/operator`), and in the
 * Creditor rep lens this renders a single lock even if it is handed traces.
 * Phase 36 replaced the seven-step card with this design (user's pick of four
 * mockups); `note` replaces the empty state, e.g. when the server has
 * forgotten a call. Phase 48: price moves are titled by their ladder step,
 * an open rep turn shows which model read the line ("Read by Claude Haiku"),
 * and the working says when code opened the reply with an acknowledgement.
 */
import { ChevronRight, ScanText } from "lucide-react";
import { type ReactNode, useState } from "react";
import { CurveSparkline } from "@/components/CurveSparkline";
import { PrivateLock, PrivateTag } from "@/components/PrivateLock";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/cn";
import { fieldLabel, termValue } from "@/lib/format";
import { splitByQuotes, splitTemplate } from "@/lib/highlight";
import { STATUS_LABEL } from "@/lib/labels";
import type { Lens } from "@/lib/lens";
import {
  type Afford,
  ackLine,
  affordLine,
  checkSummary,
  heardLines,
  ignoredLines,
  keyNumber,
  readerDetail,
  readerLabel,
  spokenText,
  turnTitle,
} from "@/lib/traceStory";
import type { TurnTraceEvent } from "@/types/protocol";

export interface DecisionTraceProps {
  traces: TurnTraceEvent[];
  lens: Lens;
  /** Shown instead of the empty-state text, e.g. why this call has no trace. */
  note?: string;
}

export const TRACE_TITLE = "How the agent decided";
export const TRACE_SUBTITLE = "Every decision comes from code; the AI handles only the language.";

/** The few-word reason for a collapsed row; older frames have only the full sentence. */
export function shortReason(t: TurnTraceEvent): string {
  return t.decide.reason_short ?? t.decide.reason_text;
}

export function DecisionTrace({ traces, lens, note }: DecisionTraceProps) {
  if (lens === "creditor") return <PrivateLock what="The decision trace" />;
  const ordered = [...traces].sort((a, b) => b.turn - a.turn);
  return (
    <Card className="flex min-h-0 flex-col">
      <div className="flex flex-col gap-0.5 px-4 pt-4 pb-3">
        <h2 className="text-base font-semibold">{TRACE_TITLE}</h2>
        <p className="m-0 text-sm text-muted">{TRACE_SUBTITLE}</p>
      </div>
      <div className="px-4 pb-4">
        {ordered.length === 0 ? (
          <p className="mx-auto max-w-[56ch] py-10 text-center text-pretty text-muted">
            {note ??
              "Each turn of the call appears here: what the rep said, what the agent took from it, whether the client can pay, what the agent decided and why, and what it said back."}
          </p>
        ) : (
          <TurnList traces={ordered} />
        )}
      </div>
    </Card>
  );
}

function TurnList({ traces }: { traces: TurnTraceEvent[] }) {
  const latest = traces[0]?.turn ?? null;
  // The newest turn is open until the visitor opens or closes a row; then their choice wins.
  const [open, setOpen] = useState<Set<number> | null>(null);
  const isOpen = (turn: number) => (open ? open.has(turn) : turn === latest);
  const toggle = (turn: number) => {
    const next = new Set(open ?? (latest != null ? [latest] : []));
    if (next.has(turn)) next.delete(turn);
    else next.add(turn);
    setOpen(next);
  };
  return (
    <ol aria-label="Turns, newest first" className="m-0 flex flex-col divide-y divide-border rounded-xl border border-border bg-surface p-0">
      {traces.map((t) => {
        const expanded = isOpen(t.turn);
        const num = keyNumber(t);
        const panelId = `turn-${t.turn}-detail`;
        return (
          <li key={t.turn} className="list-none" data-testid="turn-card" aria-label={`Turn ${t.turn}`}>
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={panelId}
              onClick={() => toggle(t.turn)}
              className="grid w-full cursor-pointer grid-cols-[1rem_2.25rem_minmax(0,1fr)_auto] items-baseline gap-x-2 px-3 py-2.5 text-left hover:bg-surface-2"
            >
              <ChevronRight aria-hidden className={cn("mt-0.5 h-4 w-4 self-start text-muted transition-transform", expanded && "rotate-90")} />
              <span className="text-xs text-muted num">
                <span className="sr-only">Turn </span>
                <span aria-hidden>T</span>
                {t.turn}
              </span>
              <span className="min-w-0">
                <span className="block font-medium">{turnTitle(t, traces)}</span>
                {!expanded && <span className="line-clamp-2 block text-sm text-muted">{shortReason(t)}</span>}
              </span>
              {num ? <span className="font-semibold num">{num}</span> : <span />}
            </button>
            {expanded && (
              <div id={panelId} className="flex flex-col gap-3 px-3 pt-1 pb-4 sm:pl-[4.75rem]">
                <Story trace={t} />
                <Disclosure summary="How this was worked out">
                  <Internals trace={t} />
                </Disclosure>
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

// ---------------------------------------------------------------- the story

function Quote({ trace }: { trace: TurnTraceEvent }) {
  const quotes = [...trace.terms.map((t) => t.quote), ...(trace.ask_quote ? [trace.ask_quote] : [])];
  return (
    <>
      {splitByQuotes(trace.creditor_text ?? "", quotes).map((s, i) =>
        s.hit ? (
          <mark key={i} className="rounded bg-hl px-0.5 text-fg">
            {s.text}
          </mark>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </>
  );
}

function AffordText({ afford }: { afford: NonNullable<Afford> }) {
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-2">
      <span className={cn(afford.tone === "good" && "text-good", afford.tone === "bad" && "text-bad")}>{afford.text}</span>
      <PrivateTag />
    </span>
  );
}

/** Which model (or code, or the simulated rep's script) read the rep's line. Negotiator view only. */
function ReaderBadge({ trace }: { trace: TurnTraceEvent }) {
  const label = readerLabel(trace.reader);
  if (!label) return null;
  return (
    <Badge className="mt-1.5 w-fit" title={readerDetail(trace.reader)} data-testid="reader-badge">
      <ScanText aria-hidden className="h-3.5 w-3.5" />
      {label}
    </Badge>
  );
}

/** Five plain lines; a line with nothing to say is left out. */
function Story({ trace }: { trace: TurnTraceEvent }) {
  const heard = heardLines(trace);
  const afford = affordLine(trace);
  const rows: [string, ReactNode][] = [];
  if (trace.creditor_text) {
    rows.push([
      "What they said",
      <span key="q" className="flex flex-col items-start">
        <q className="before:content-['“'] after:content-['”']">
          <Quote trace={trace} />
        </q>
        <ReaderBadge trace={trace} />
      </span>,
    ]);
  }
  if (heard.length > 0) rows.push(["What we heard", heard.join(". ") + "."]);
  if (afford) rows.push(["Can the client pay?", <AffordText key="a" afford={afford} />]);
  rows.push(["Decision", trace.decide.reason_text]);
  rows.push([
    "What we said",
    <span key="s" className="font-medium">
      {spokenText(trace)}
    </span>,
  ]);
  return (
    <dl className="m-0 grid grid-cols-1 gap-x-4 gap-y-2.5 sm:grid-cols-[9.5rem_minmax(0,1fr)]">
      {rows.map(([label, body]) => (
        <div key={label} className="contents">
          <dt className="text-sm text-muted">{label}</dt>
          <dd className="m-0 -mt-2 sm:mt-0">{body}</dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------- the details

function Disclosure({ summary, children }: { summary: string; children: ReactNode }) {
  return (
    <details className="group rounded-lg border border-border">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-sm font-medium text-muted hover:text-fg [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden className="h-4 w-4 transition-transform group-open:rotate-90" />
        {summary}
      </summary>
      <div className="border-t border-border px-3 py-3">{children}</div>
    </details>
  );
}

/** Everything the story leaves out, for whoever wants to check the working. */
function Internals({ trace }: { trace: TurnTraceEvent }) {
  const c = checkSummary(trace);
  const ignored = ignoredLines(trace);
  const ack = ackLine(trace);
  return (
    <div className="flex flex-col gap-4 text-sm">
      {trace.affordability && (
        <section>
          <h5 className="mb-1 font-medium">Which settlements the client can afford</h5>
          <CurveSparkline
            curve={trace.affordability.curve}
            maxBp={trace.affordability.max_bp}
            askBp={trace.ask_bp}
            counterBp={trace.counter_bp}
          />
        </section>
      )}
      {trace.belief_changes.length > 0 && (
        <section>
          <h5 className="mb-1 font-medium">What the agent now knows</h5>
          <ul className="m-0 flex flex-col gap-0.5 p-0">
            {trace.belief_changes.map((b) => (
              <li key={b.field} className="list-none">
                {fieldLabel(b.field)}: <span className="num">{termValue(b.field, b.new_value)}</span>{" "}
                <span className="text-muted">({STATUS_LABEL[b.new_status].toLowerCase()})</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {ignored.length > 0 && (
        <section>
          <h5 className="mb-1 font-medium">What the agent set aside</h5>
          <ul className="m-0 flex flex-col gap-0.5 p-0 text-muted">
            {ignored.map((l) => (
              <li key={l} className="list-none">
                {l}
              </li>
            ))}
          </ul>
        </section>
      )}
      <section>
        <h5 className="mb-1 font-medium">The reply before the numbers went in</h5>
        {ack && <p className="m-0 mb-1.5 text-xs text-muted text-pretty">{ack}</p>}
        <p className="m-0 font-mono text-xs leading-relaxed" data-testid="reply-template">
          {splitTemplate(trace.nlg.template).map((s, i) =>
            s.hit ? (
              <span key={i} className="rounded bg-accent-soft px-1 text-accent" data-placeholder>
                {s.text}
              </span>
            ) : (
              <span key={i}>{s.text}</span>
            ),
          )}
        </p>
        <p className="m-0 mt-1 text-xs text-muted">
          {trace.nlg.mode === "llm" ? "Worded by the AI from this outline." : "A fixed template."} Code fills each blank
          with a checked figure.
        </p>
      </section>
      <section>
        <h5 className="mb-1 font-medium">Safety checks on the reply</h5>
        <ul className="m-0 flex flex-col gap-0.5 p-0">
          {c.passed.map((p) => (
            <li key={p} className="list-none text-muted">
              <span aria-hidden>✓ </span>
              {p}
            </li>
          ))}
          {c.blocked.map((b) => (
            <li key={b} className="list-none text-warn">
              <span aria-hidden>✕ </span>
              {b}
            </li>
          ))}
          {c.fallback && <li className="list-none text-warn">The safe template was spoken instead of the AI's wording.</li>}
        </ul>
      </section>
      <p className="m-0 text-xs text-muted">
        Policy code: <code className="font-mono">{trace.decide.intent}</code>
        {trace.decide.reason && (
          <>
            {" "}
            <code className="font-mono">{trace.decide.reason}</code>
          </>
        )}
      </p>
    </div>
  );
}
