/**
 * Feasibility curve for one turn: which settlement percentages (1–100%) the
 * engine found affordable, with the creditor's ask, our counter, and the
 * dashed PRIVATE ceiling (max affordable). Operator lens only; the caller
 * renders a lock instead in the creditor lens.
 */
import { Area, AreaChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { PrivateTag } from "@/components/PrivateLock";
import { pct } from "@/lib/format";
import type { CurvePoint } from "@/types/events";

export interface CurveSparklineProps {
  curve: CurvePoint[];
  maxBp: number | null;
  askBp: number | null;
  counterBp: number | null;
}

export function CurveSparkline({ curve, maxBp, askBp, counterBp }: CurveSparklineProps) {
  const data = curve.map((p) => ({ x: p.bp / 100, ok: p.feasible ? 1 : 0 }));
  const feasibleCount = curve.filter((p) => p.feasible).length;
  return (
    <figure className="m-0">
      <div className="h-16 w-full" role="img" aria-label={`Feasibility curve: ${feasibleCount} of ${curve.length} settlement percentages affordable`}>
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 320, height: 64 }}>
          <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
            <XAxis dataKey="x" type="number" domain={[1, 100]} hide />
            <YAxis domain={[0, 1]} hide />
            <Tooltip
              cursor={{ stroke: "var(--faint)" }}
              formatter={(v) => (v === 1 ? "affordable" : "not affordable")}
              labelFormatter={(x) => `${String(x)}% of balance`}
              contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 }}
            />
            <Area
              type="stepAfter"
              dataKey="ok"
              name="Feasible"
              stroke="var(--accent)"
              strokeWidth={1.5}
              fill="var(--accent-soft)"
              isAnimationActive={false}
            />
            {maxBp != null && (
              <ReferenceLine x={maxBp / 100} stroke="var(--muted)" strokeDasharray="4 3" strokeWidth={1.5} />
            )}
            {askBp != null && <ReferenceLine x={askBp / 100} stroke="var(--series-ask)" strokeWidth={2} />}
            {counterBp != null && <ReferenceLine x={counterBp / 100} stroke="var(--accent)" strokeWidth={2} />}
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted num">
        <span>1%</span>
        {askBp != null && (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-3 w-0.5 bg-ask" aria-hidden /> Ask {pct(askBp)}
          </span>
        )}
        {counterBp != null && (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-3 w-0.5 bg-accent" aria-hidden /> Ours {pct(counterBp)}
          </span>
        )}
        {maxBp != null && (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-3 w-0 border-l-2 border-dashed border-muted" aria-hidden /> Ceiling {pct(maxBp)}
            <PrivateTag />
          </span>
        )}
        <span className="ml-auto">100%</span>
      </figcaption>
    </figure>
  );
}
