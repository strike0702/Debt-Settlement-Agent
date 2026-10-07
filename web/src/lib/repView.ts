/**
 * Client-side mirror of the server's rep ("Creditor rep" view) stream filter,
 * `redact_for_view` in app/voice/views.py.
 *
 * Live rep calls are filtered by the server. This exists for fixture mode
 * (which replays an operator recording) and for switching to the Creditor
 * rep view in the middle of a Debt negotiator call. The rep stream carries no
 * decision trace, so `turn_trace` is dropped. Components also refuse to render
 * private fields in the creditor lens, so a leak needs both layers to fail.
 */
import type { CallEvent } from "@/lib/callState";
import type { EvalEvent, ScheduleRow } from "@/types/protocol";

function publicRow(r: ScheduleRow): ScheduleRow {
  return { date: r.date, creditor_payment_cents: r.creditor_payment_cents };
}

/** Return the event as the rep stream would send it, or null to drop it. */
export function toRepView(ev: CallEvent): CallEvent | null {
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
    case "blocked": {
      const { offending: _private, ...rest } = ev;
      return rest;
    }
    case "turn_trace":
      return null;
    case "audit":
      return ev.private ? null : ev;
    default:
      return ev;
  }
}
