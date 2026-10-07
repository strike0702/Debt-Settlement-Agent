/**
 * Center column, the hero: one card per agent turn showing how the move was
 * made. Order matches the pipeline: rep line → NLU → belief → engine →
 * policy → NLG template + guards → spoken line.
 *
 * Reads `turn_trace` events only. Debt negotiator view only (Phase 35): the
 * rep stream carries no traces, and in the Creditor rep lens this renders a
 * single lock even if it is handed traces. When the engine did not run, step 4
 * says why (opening line, rules still missing via `needs_info`, or a call that
 * streams the Creditor rep view, via `note`).
 */
import { ArrowRight, Check, ShieldAlert, X } from "lucide-react";
import type { ReactNode } from "react";
import { CurveSparkline } from "@/components/CurveSparkline";
import { PrivateLock } from "@/components/PrivateLock";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import type { Lens } from "@/lib/lens";
import { cn } from "@/lib/cn";
import { fieldLabel, pct, termValue } from "@/lib/format";
import { splitByQuotes, splitTemplate } from "@/lib/highlight";
import { STANCE_TONE, stanceText } from "@/lib/stance";
import type { TurnTraceEvent } from "@/types/protocol";

export interface DecisionTraceProps {
  traces: TurnTraceEvent[];
  lens: Lens;
  /** Shown instead of the empty-state text, e.g. why this call has no trace. */
  note?: string;
}

/** Why step 4 has no curve, in plain words. */
export function engineNote(trace: Pick<TurnTraceEvent, "creditor_text" | "needs_info">): string {
  if (trace.creditor_text == null) return "No engine run: the agent is opening or closing the call.";
  const missing = trace.needs_info ?? null;
  if (missing && missing.length > 0) {
    return `Waiting for: ${missing.map((f) => fieldLabel(f).toLowerCase()).join(", ")}. The engine runs once the rep's rules are known.`;
  }
  if (missing) return "No engine run this turn: the agent asked a clarifying question first.";
  return "No engine run this turn.";
}

export function DecisionTrace({ traces, lens, note }: DecisionTraceProps) {
  if (lens === "creditor") return <PrivateLock what="The decision trace" />;
  const ordered = [...traces].sort((a, b) => b.turn - a.turn);
  return (
    <Card className="flex min-h-0 flex-col">
      <CardHeader
        title="Decision trace"
        aside={<span className="text-sm text-muted">Code decides. The LLM only phrases.</span>}
      />
      <div className="flex flex-col gap-4 px-4 pb-4" aria-label="Decision trace, newest turn first">
        {ordered.length === 0 ? (
          <p className="py-10 text-center text-muted">
            {note ?? <>Each agent turn appears here: what the rep said, what was understood, what the engine
            allowed, why the policy chose its move, and how the line was guarded.</>}
          </p>
        ) : (
          ordered.map((t, i) => <TurnCard key={t.turn} trace={t} latest={i === 0} />)
        )}
      </div>
    </Card>
  );
}

function Step({ n, title, children }: { n: number; title: ReactNode; children: ReactNode }) {
  return (
    <section className="grid grid-cols-[1.5rem_1fr] gap-x-3 gap-y-1">
      <span className="mt-0.5 flex h-6 w-6 items-center justify-center rounded-full bg-surface-2 text-xs font-semibold text-muted num">
        {n}
      </span>
      <div className="min-w-0">
        <h4 className="text-sm font-semibold text-muted">{title}</h4>
        <div className="mt-1">{children}</div>
      </div>
    </section>
  );
}

export function TurnCard({ trace, latest }: { trace: TurnTraceEvent; latest: boolean }) {
  const quotes = [...trace.terms.map((t) => t.quote), ...(trace.ask_quote ? [trace.ask_quote] : [])];
  const guardsOk = trace.nlg.guards.every((g) => g.ok);
  const said = stanceText(trace.stance);
  const tone = trace.stance ? STANCE_TONE[trace.stance] : undefined;
  return (
    <article
      data-testid="turn-card"
      aria-label={`Turn ${trace.turn}`}
      className={cn(
        "flex flex-col gap-4 rounded-xl border p-4",
        latest ? "border-accent bg-surface" : "border-border bg-surface",
      )}
    >
      <header className="flex flex-wrap items-center gap-2">
        <h3 className="text-base font-semibold num">Turn {trace.turn}</h3>
        <Badge tone="accent">{trace.decide.intent}</Badge>
      </header>

      <Step
        n={1}
        title={
          <>
            Creditor rep said
            {said && (
              <>
                {" · "}
                <span data-testid="rep-stance" className={cn("font-normal", tone === "good" ? "text-good" : tone === "warn" ? "text-warn" : "text-fg")}>
                  {said}
                </span>
              </>
            )}
          </>
        }
      >
        {trace.creditor_text ? (
          <blockquote className="m-0 border-l-2 border-ask pl-3">
            {splitByQuotes(trace.creditor_text, quotes).map((s, i) =>
              s.hit ? (
                <mark key={i} className="rounded bg-hl px-0.5 text-fg">
                  {s.text}
                </mark>
              ) : (
                <span key={i}>{s.text}</span>
              ),
            )}
          </blockquote>
        ) : (
          <p className="text-muted">Agent opens the call.</p>
        )}
      </Step>

      <Step n={2} title="Understood (NLU)">
        {trace.terms.length === 0 && trace.dropped.length === 0 && trace.ask_bp == null ? (
          <p className="text-muted">No terms in this line.</p>
        ) : (
          <ul className="flex flex-col gap-1 text-sm">
            {trace.ask_bp != null && (
              <li>
                Settlement ask <strong className="num">{pct(trace.ask_bp)}</strong>
              </li>
            )}
            {trace.terms.map((t) => (
              <li key={t.field} className="flex flex-wrap items-center gap-x-2">
                <span>{fieldLabel(t.field)}</span>
                <strong className="num">{termValue(t.field, t.value)}</strong>
                {t.hedged && <Badge tone="warn">hedged</Badge>}
                <Check aria-label="verified against the quote" className="h-3.5 w-3.5 text-good" />
              </li>
            ))}
            {trace.dropped.map((d) => (
              <li key={`drop-${d.field}`} className="flex flex-wrap items-center gap-x-2 text-muted">
                <s>
                  {fieldLabel(d.field)} {termValue(d.field, d.value)}
                </s>
                <Badge tone="bad">dropped: {d.reason.replaceAll("_", " ")}</Badge>
              </li>
            ))}
          </ul>
        )}
      </Step>

      <Step n={3} title="Belief changes">
        {trace.belief_changes.length === 0 ? (
          <p className="text-sm text-muted">None.</p>
        ) : (
          <ul className="flex flex-col gap-1 text-sm">
            {trace.belief_changes.map((c) => (
              <li key={c.field} className="flex flex-wrap items-center gap-x-2">
                <span>{fieldLabel(c.field)}</span>
                <span className="text-muted num">{termValue(c.field, c.old_value)}</span>
                <ArrowRight aria-label="becomes" className="h-3.5 w-3.5 text-muted" />
                <strong className="num">{termValue(c.field, c.new_value)}</strong>
                <span className="text-xs text-muted">
                  {c.old_status.toLowerCase()} → {c.new_status.toLowerCase()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Step>

      <Step n={4} title="Engine: what the client can afford">
        {trace.affordability ? (
          <CurveSparkline
            curve={trace.affordability.curve}
            maxBp={trace.affordability.max_bp}
            askBp={trace.ask_bp}
            counterBp={trace.counter_bp}
          />
        ) : (
          <p className="text-sm text-muted" data-testid="engine-note">{engineNote(trace)}</p>
        )}
      </Step>

      <Step n={5} title="Policy decision">
        <p>{trace.decide.reason_text}</p>
        <p className="mt-0.5 font-mono text-xs text-muted">
          {trace.decide.intent} · {trace.decide.reason}
        </p>
      </Step>

      <Step n={6} title={`Phrasing (${trace.nlg.mode === "llm" ? "LLM, guarded" : "template"})`}>
        <p className="font-mono text-sm leading-relaxed">
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
        <div className="mt-2 flex flex-wrap gap-1.5">
          {trace.nlg.guards.map((g, i) => (
            // A stage can repeat (LLM attempt, then the template fallback).
            <Badge key={`${g.stage}-${i}`} tone={g.ok ? "good" : "bad"}>
              {g.ok ? <Check aria-hidden className="h-3 w-3" /> : <X aria-hidden className="h-3 w-3" />}
              {g.stage} {g.ok ? "passed" : `blocked: ${(g.reason ?? "").replaceAll("_", " ")}`}
            </Badge>
          ))}
          {trace.nlg.fallback_used && (
            <Badge tone="warn">
              <ShieldAlert aria-hidden className="h-3 w-3" /> template fallback spoken
            </Badge>
          )}
        </div>
        <span className="sr-only">{guardsOk ? "All guards passed." : "A guard blocked the LLM phrasing."}</span>
      </Step>

      <Step n={7} title="Spoken">
        <p className="text-base font-medium">{trace.spoken.map((s) => s.text).join(" ")}</p>
      </Step>
    </article>
  );
}
