/**
 * Right column: the state of the negotiation after the latest turn.
 *
 * Order (Phase 36, both views): the outcome first ("Agreement drafted" once
 * agreed, then "Proposed schedule"), then `context` (App's scenario brief and
 * client ledger in the Debt negotiator view, "Your account" in the Creditor
 * rep view), then the working detail. Debt negotiator view adds the ladder
 * chart (ask vs our offers, private ceiling), the belief table, the per-turn
 * latency waterfall and a collapsible audit log. Creditor rep view (Phase 35):
 * only the agreed terms (agreement, schedule without private columns, terms
 * heard); the ladder, latency and audit are the negotiator's tools.
 */
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { type ReactNode, useState } from "react";
import { PrivateLock, PrivateTag } from "@/components/PrivateLock";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import type { CallState } from "@/lib/callState";
import { ladderPoints } from "@/lib/callState";
import { fieldLabel, isoDate, money, ms, pct, termValue } from "@/lib/format";
import { STATUS_LABEL } from "@/lib/labels";
import { waterfall } from "@/lib/latency";
import type { Lens } from "@/lib/lens";
import type { AuditEvent, BeliefTerm, ScheduleRow, TurnTraceEvent } from "@/types/protocol";

const TOOLTIP_STYLE = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  fontSize: 12,
  color: "var(--fg)",
};
const AXIS_TICK = { fill: "var(--muted)", fontSize: 12 };

export function StatePanel({ state, lens, context }: { state: CallState; lens: Lens; context?: ReactNode }) {
  const latestCeiling = lens === "operator" ? (state.traces.at(-1)?.affordability?.max_bp ?? null) : null;
  return (
    <div className="flex flex-col gap-4">
      {state.agreement && (
        <Card className="border-good">
          <CardHeader title="Agreement drafted" aside={<Badge tone="good">Pending client approval</Badge>} />
          <CardBody>
            <p className="text-lg num">
              <strong>{pct(state.agreement.bp)}</strong> of the balance,{" "}
              <strong>{money(state.agreement.offer_total)}</strong> to {state.agreement.creditor}
            </p>
          </CardBody>
        </Card>
      )}
      <ScheduleCard rows={state.evaluation?.rows ?? null} lens={lens} programFee={lens === "operator" ? state.evaluation?.program_fee_cents ?? null : null} />
      {context}
      {lens === "operator" && <LadderCard traces={state.traces} ceiling={latestCeiling} lens={lens} />}
      <BeliefCard terms={state.belief} lens={lens} />
      {lens === "operator" && <LatencyCard traces={state.traces} />}
      {lens === "operator" && <AuditCard rows={state.audit} lens={lens} />}
    </div>
  );
}

function LadderCard({ traces, ceiling, lens }: { traces: TurnTraceEvent[]; ceiling: number | null; lens: Lens }) {
  const data = ladderPoints(traces).map((p) => ({
    turn: p.turn,
    ask: p.ask == null ? null : p.ask / 100,
    ours: p.counter == null ? null : p.counter / 100,
  }));
  return (
    <Card>
      <CardHeader title="Negotiation ladder" aside={<span className="text-sm text-muted">% of balance by turn</span>} />
      <CardBody>
        {data.length === 0 ? (
          <p className="text-sm text-muted">Offers appear once a percentage is on the table.</p>
        ) : (
          <div className="h-48" role="img" aria-label="Creditor ask and our offers by turn">
            <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 360, height: 192 }}>
              <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
                <CartesianGrid stroke="var(--border)" vertical={false} />
                <XAxis dataKey="turn" tick={AXIS_TICK} tickLine={false} axisLine={{ stroke: "var(--border)" }} tickFormatter={(t) => `T${String(t)}`} />
                <YAxis domain={[0, 60]} tick={AXIS_TICK} tickLine={false} axisLine={false} unit="%" />
                <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => `${String(v)}%`} labelFormatter={(t) => `Turn ${String(t)}`} />
                <Legend wrapperStyle={{ fontSize: 12, color: "var(--muted)" }} />
                <Line type="monotone" dataKey="ask" name="Creditor ask" stroke="var(--series-ask)" strokeWidth={2} dot={{ r: 4 }} connectNulls isAnimationActive={false} />
                <Line type="monotone" dataKey="ours" name="Our offer" stroke="var(--accent)" strokeWidth={2} dot={{ r: 4 }} connectNulls isAnimationActive={false} />
                {ceiling != null && (
                  <ReferenceLine y={ceiling / 100} stroke="var(--muted)" strokeDasharray="4 3" strokeWidth={1.5} />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
        {lens === "operator" && ceiling != null && (
          <p className="mt-2 flex items-center gap-2 text-xs text-muted num">
            <span className="h-0 w-4 border-t-2 border-dashed border-muted" aria-hidden />
            Ceiling {pct(ceiling)} <PrivateTag />
          </p>
        )}
        {lens === "creditor" && <PrivateLock className="mt-2" what="The client's ceiling" />}
      </CardBody>
    </Card>
  );
}

function ScheduleCard({ rows, lens, programFee }: { rows: ScheduleRow[] | null; lens: Lens; programFee: number | null }) {
  const op = lens === "operator";
  return (
    <Card>
      <CardHeader title="Proposed schedule" />
      <CardBody>
        {!rows || rows.length === 0 ? (
          <p className="text-sm text-muted">No schedule yet. It appears once the agent has terms the client can afford.</p>
        ) : (
          <div className="overflow-x-auto">
            {/* Phones: 12 px type so the five operator columns fit 390 px. */}
            <table className="w-full text-xs num sm:text-sm">
              <thead className="text-left text-muted">
                <tr>
                  <th className="py-1 pr-3 font-medium">Date</th>
                  <th className="py-1 pr-3 text-right font-medium">Payment</th>
                  {op && (
                    <>
                      <th className="py-1 pr-3 text-right font-medium">Fee</th>
                      <th className="py-1 pr-3 text-right font-medium">Bank</th>
                      <th className="py-1 text-right font-medium">Savings after</th>
                    </>
                  )}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.date} className="border-t border-border">
                    <td className="py-1.5 pr-3 whitespace-nowrap">{isoDate(r.date)}</td>
                    <td className="py-1.5 pr-3 text-right">{money(r.creditor_payment_cents)}</td>
                    {op && (
                      <>
                        <td className="py-1.5 pr-3 text-right">{money(r.program_fee_cents)}</td>
                        <td className="py-1.5 pr-3 text-right">{money(r.bank_fee_cents)}</td>
                        <td className="py-1.5 text-right">{money(r.balance_cents)}</td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t border-border font-semibold">
                  <td className="py-1.5 pr-3">Total</td>
                  <td className="py-1.5 pr-3 text-right">
                    {money(rows.reduce((s, r) => s + r.creditor_payment_cents, 0))}
                  </td>
                  {op && (
                    <td className="py-1.5 pr-3 text-right" colSpan={3}>
                      <span className="inline-flex items-center gap-2">
                        Fees {money(programFee)} <PrivateTag />
                      </span>
                    </td>
                  )}
                </tr>
              </tfoot>
            </table>
          </div>
        )}
        {!op && rows && rows.length > 0 && <PrivateLock className="mt-3" what="The fee and savings detail" />}
      </CardBody>
    </Card>
  );
}

const STATUS_TONE = {
  KNOWN: "good",
  TENTATIVE: "warn",
  CONTRADICTED: "bad",
  ASSUMED: "neutral",
  UNKNOWN: "neutral",
} as const;

function BeliefCard({ terms, lens }: { terms: BeliefTerm[]; lens: Lens }) {
  return (
    <Card>
      <CardHeader title="Terms we've heard" />
      <CardBody>
        {terms.length === 0 ? (
          <p className="text-sm text-muted">Nothing heard yet. Terms appear as the rep states them.</p>
        ) : (
          <table className="w-full text-sm">
            <tbody>
              {terms.map((t) => (
                <tr key={t.field} className="border-t border-border first:border-t-0">
                  <td className="py-1.5 pr-3 text-muted">{fieldLabel(t.field)}</td>
                  <td className="py-1.5 pr-3 num">{termValue(t.field, t.value)}</td>
                  <td className="py-1.5 text-right">
                    {lens === "operator" ? (
                      <Badge tone={STATUS_TONE[t.status]}>{STATUS_LABEL[t.status]}</Badge>
                    ) : (
                      t.status === "ASSUMED" && <span className="text-xs text-muted">Default</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardBody>
    </Card>
  );
}

function LatencyCard({ traces }: { traces: TurnTraceEvent[] }) {
  const withTimings = traces.filter((t) => Object.keys(t.timings).length > 0);
  const [picked, setPicked] = useState<number | null>(null);
  const trace = withTimings.find((t) => t.turn === picked) ?? withTimings.at(-1);
  const rows = trace ? waterfall(trace.timings) : [];
  const total = rows.reduce((s, r) => s + r.ms, 0);
  return (
    <Card>
      <CardHeader
        title="Latency"
        aside={
          withTimings.length > 0 && (
            <label className="flex items-center gap-2 text-sm text-muted">
              <span className="sr-only">Turn</span>
              <select
                className="rounded-md border border-border bg-surface px-2 py-1 text-sm text-fg"
                value={trace?.turn ?? ""}
                onChange={(e) => setPicked(Number(e.target.value))}
              >
                {withTimings.map((t) => (
                  <option key={t.turn} value={t.turn}>
                    Turn {t.turn}
                  </option>
                ))}
              </select>
            </label>
          )
        }
      />
      <CardBody>
        {rows.length === 0 ? (
          <p className="text-sm text-muted">Per-stage timings appear after the first turn.</p>
        ) : (
          <>
            <div className="h-48" role="img" aria-label={`Latency waterfall for turn ${String(trace?.turn)}, ${ms(total)} total`}>
              <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 360, height: 192 }}>
                <BarChart data={rows} layout="vertical" margin={{ top: 0, right: 12, bottom: 0, left: 0 }} barCategoryGap={4}>
                  <XAxis type="number" tick={AXIS_TICK} tickLine={false} axisLine={{ stroke: "var(--border)" }} unit=" ms" />
                  <YAxis type="category" dataKey="stage" width={120} tick={AXIS_TICK} tickLine={false} axisLine={false} />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    cursor={{ fill: "var(--surface-2)" }}
                    formatter={(v, name) => (name === "ms" ? [ms(Number(v)), "duration"] : [null, null])}
                  />
                  <Bar dataKey="start" stackId="w" fill="transparent" isAnimationActive={false} />
                  <Bar dataKey="ms" stackId="w" fill="var(--accent)" radius={[0, 4, 4, 0]} minPointSize={2} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <table className="mt-2 w-full text-xs text-muted num">
              <caption className="sr-only">Stage timings</caption>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.stage}>
                    <td className="pr-3">{r.stage}</td>
                    <td className="text-right">{ms(r.ms)}</td>
                  </tr>
                ))}
                <tr className="font-semibold text-fg">
                  <td className="pr-3">To first audio</td>
                  <td className="text-right">{ms(total)}</td>
                </tr>
              </tbody>
            </table>
          </>
        )}
      </CardBody>
    </Card>
  );
}

function AuditCard({ rows, lens }: { rows: AuditEvent[]; lens: Lens }) {
  // Defense in depth: the rep stream already drops private rows.
  const visible = lens === "creditor" ? rows.filter((r) => !r.private) : rows;
  return (
    <Card>
      <details>
        <summary className="flex cursor-pointer items-center justify-between px-4 py-4 text-base font-semibold">
          Audit log
          <span className="text-sm font-normal text-muted num">{visible.length} rows</span>
        </summary>
        <ol className="max-h-80 overflow-y-auto px-4 pb-4 font-mono text-xs">
          {visible.map((r) => (
            <li key={r.id} className="flex gap-2 border-t border-border py-1.5">
              <span className="w-8 shrink-0 text-faint num">{r.id}</span>
              <span className="w-20 shrink-0 text-muted">{r.actor}</span>
              <span className="min-w-0 break-words">
                <span className="text-fg">{r.event}</span>{" "}
                <span className="text-muted">{r.payload ? JSON.stringify(r.payload) : ""}</span>
                {r.private && <PrivateTag className="ml-2" />}
              </span>
            </li>
          ))}
        </ol>
      </details>
    </Card>
  );
}
