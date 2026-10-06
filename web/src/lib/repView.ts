/**
 * Client-side mirror of the Phase 22 rep ("creditor's eye") stream filter.
 *
 * The server filters the live rep stream; this exists so fixture mode, which
 * replays an operator recording, shows exactly what a rep stream would carry.
 * Components also refuse to render private fields in the creditor lens, so a
 * leak needs both this filter and a component to fail.
 */
import type { EvalEvent, ScheduleRow, ServerEvent } from "@/types/events";

function publicRow(r: ScheduleRow): ScheduleRow {
  return { date: r.date, creditor_payment_cents: r.creditor_payment_cents };
}

/** Return the event as the rep stream would send it, or null to drop it. */
export function toRepView(ev: ServerEvent): ServerEvent | null {
  switch (ev.type) {
    case "eval": {
      const out: EvalEvent = {
        type: "eval",
        feasible: ev.feasible,
        shape: ev.shape,
        offer_total_cents: ev.offer_total_cents,
        assumed_fields: ev.assumed_fields,
        agreed_bp: ev.agreed_bp,
        rows: ev.rows ? ev.rows.map(publicRow) : null,
      };
      return out;
    }
    case "agreement":
      return { ...ev, rows: ev.rows.map(publicRow) };
    case "turn_trace": {
      const { affordability: _private, ...rest } = ev;
      return rest;
    }
    case "audit":
      return ev.private ? null : ev;
    default:
      return ev;
  }
}
