/**
 * WebSocket protocol for `/ws/call/{call_id}`, hand-written.
 *
 * REPLACED BY GENERATED TYPES IN 23b (from web/src/types/events.schema.json,
 * exported by `python -m app.schemas.events`). Until then this file mirrors
 * `app/voice/ws.py` as of phase 20, plus the planned `turn_trace` event from
 * REVIEW_PLAN Phase 22 task 2. Field names follow the server's snake_case.
 *
 * Units: money is integer cents (`*_cents`, and `offer_total` on agreement),
 * percentages are integer basis points (`*_bp`, 4500 = 45%), dates are ISO
 * `YYYY-MM-DD` strings. Timings are float milliseconds.
 */

export type Phase =
  | "OPENING"
  | "DISCOVERY"
  | "NEGOTIATE"
  | "CONFIRM"
  | "WRAP"
  | "ESCALATE"
  | "END";

export type Intent =
  | "OPENING"
  | "ASK"
  | "ASK_SETTLEMENT"
  | "READ_BACK"
  | "CLARIFY"
  | "REFUSE_PRIVATE"
  | "REFUSE_COMMIT"
  | "COUNTER"
  | "COUNTER_TERMS"
  | "CONFIRM_SCHEDULE"
  | "SPEAK_SCHEDULE"
  | "PROPOSE_WRAP"
  | "CLOSE"
  | "NO_DEAL_WRAP"
  | "ESCALATE";

export type TermField =
  | "max_payments"
  | "min_payment_cents"
  | "payment_structure"
  | "first_payment_date"
  | "max_segments"
  | "max_token_pays"
  | "min_payment_tiers";

export type TermStatus = "UNKNOWN" | "TENTATIVE" | "KNOWN" | "CONTRADICTED" | "ASSUMED";

export type Stance =
  | "offer"
  | "counter"
  | "accept"
  | "reject"
  | "stall"
  | "info"
  | "question"
  | "other";

/** One tier as the belief stores it: `[from_payment, min_cents]` (a Python tuple on the wire). */
export type TierValue = [number, number];
export type TermValue = number | string | null | TierValue[];

/** Which stream the socket is on (`?view=`). `rep` = creditor's eye, server-filtered. */
export type View = "rep" | "operator";

// ---------------------------------------------------------------- server → client

export interface TranscriptEvent {
  type: "transcript";
  role: "creditor" | "agent";
  text: string;
  spoken: boolean;
  sentence_id: string | null;
  blocked: boolean;
}

export interface SayEvent {
  type: "say";
  id: string;
  text: string;
}

export interface Evidence {
  turn: number;
  quote: string;
}

export interface BeliefTerm {
  field: TermField;
  value: TermValue;
  status: TermStatus;
  evidence: Evidence[];
  history: TermValue[];
}

export interface BeliefEvent {
  type: "belief";
  terms: BeliefTerm[];
}

export interface ScheduleRow {
  date: string;
  creditor_payment_cents: number;
  /** PRIVATE: firm fee. Dropped on the rep stream. */
  program_fee_cents?: number;
  /** PRIVATE: bank fee. Dropped on the rep stream. */
  bank_fee_cents?: number;
  /** PRIVATE: client's savings (SDA) balance after the draft. Dropped on the rep stream. */
  balance_cents?: number;
}

export interface FundsOption {
  amount_cents: number | null;
  within_guardrail: boolean;
  reason: string | null;
  date: string | null;
  num_drafts: number | null;
}

export interface EvalEvent {
  type: "eval";
  feasible: boolean;
  shape: string | null;
  offer_total_cents: number | null;
  /** PRIVATE: firm fee. Dropped on the rep stream (Phase 22). */
  program_fee_cents?: number | null;
  assumed_fields: TermField[];
  agreed_bp: number | null;
  rows: ScheduleRow[] | null;
  /** PRIVATE: rescue data. Dropped on the rep stream. */
  additional_funds?: { lump_sum: FundsOption; monthly_increment: FundsOption } | null;
  /** PRIVATE: highest affordable settlement. Dropped on the rep stream. */
  max_bp?: number | null;
}

export interface BlockedEvent {
  type: "blocked";
  stage: "template" | "unfilled" | "rendered" | string;
  reason: string;
  offending?: string[] | string | null;
}

export interface EscalateEvent {
  type: "escalate";
  reason: string;
  escalate_reason: string | null;
}

export interface LatencyEvent {
  type: "latency";
  turn: number;
  stt_ms: number | null;
  nlu_ms: number | null;
  policy_ms: number | null;
  nlg_ms: number | null;
  server_total_ms: number | null;
}

export interface AuditEvent {
  type: "audit";
  id: number;
  ts: string;
  actor: string;
  event: string;
  payload: Record<string, unknown> | null;
  /** Planned (Phase 22): rows the rep stream must never see. */
  private?: boolean;
}

export interface PhaseEvent {
  type: "phase";
  phase: Phase;
  intent: Intent | null;
  turn: number;
}

export interface AgreementEvent {
  type: "agreement";
  creditor: string;
  bp: number;
  /** cents */
  offer_total: number;
  status: "pending_client_approval";
  assumed_fields: TermField[];
  rows: ScheduleRow[];
}

export interface SttErrorEvent {
  type: "stt_error";
  message: string;
}

export interface ErrorEvent {
  type: "error";
  message: string;
}

export interface TurnDoneEvent {
  type: "turn_done";
}

// ---- planned: turn_trace (REVIEW_PLAN Phase 22 task 2), one per agent turn

export interface TraceTerm {
  field: TermField;
  value: TermValue;
  /** Verbatim span of the rep line; the UI highlights it. */
  quote: string;
  verified: boolean;
  hedged: boolean;
}

export interface DroppedTerm {
  field: TermField | string;
  value: TermValue;
  /** post_verify reason, e.g. "quote_not_in_utterance". */
  reason: string;
  quote: string | null;
}

export interface TraceBeliefChange {
  field: TermField;
  old_value: TermValue;
  new_value: TermValue;
  old_status: TermStatus;
  new_status: TermStatus;
  turn: number;
  quote: string | null;
}

export interface CurvePoint {
  bp: number;
  feasible: boolean;
}

/** PRIVATE. Operator stream only; absent (not null-filled) on the rep stream. */
export interface Affordability {
  max_bp: number | null;
  curve: CurvePoint[];
}

export interface Decide {
  intent: Intent;
  /** Machine reason code from `decide()`, e.g. "bp=4900", "confirmed". */
  reason: string;
  /** Plain-English sentence from app/agent/reasons.py, public values only. */
  reason_text: string;
}

export interface GuardResult {
  stage: "template" | "rendered" | "unfilled" | string;
  ok: boolean;
  reason: string | null;
}

export interface NlgTrace {
  mode: "template" | "llm" | string;
  /** Text with `{placeholder}` slots before Fact.render() fills them. */
  template: string;
  guards: GuardResult[];
  fallback_used: boolean;
}

export interface TraceTimings {
  stt_ms?: number | null;
  queue_ms?: number | null;
  nlu_ms?: number | null;
  engine_ms?: number | null;
  policy_ms?: number | null;
  nlg_ms?: number | null;
  tts_onset_ms?: number | null;
  server_total_ms?: number | null;
}

export interface TurnTraceEvent {
  type: "turn_trace";
  turn: number;
  creditor_text: string | null;
  stance: Stance | null;
  /** Creditor's settlement ask this turn, bp (public: the rep said it). */
  ask_bp: number | null;
  ask_quote: string | null;
  terms: TraceTerm[];
  dropped: DroppedTerm[];
  belief_changes: TraceBeliefChange[];
  affordability?: Affordability | null;
  decide: Decide;
  /** Our counter or confirmed bp if this move spoke one (public once said). */
  counter_bp: number | null;
  nlg: NlgTrace;
  spoken: { id: string; text: string }[];
  timings: TraceTimings;
}

export type ServerEvent =
  | TranscriptEvent
  | SayEvent
  | BeliefEvent
  | EvalEvent
  | BlockedEvent
  | EscalateEvent
  | LatencyEvent
  | AuditEvent
  | PhaseEvent
  | AgreementEvent
  | SttErrorEvent
  | ErrorEvent
  | TurnDoneEvent
  | TurnTraceEvent;

export type ServerEventType = ServerEvent["type"];

// ---------------------------------------------------------------- client → server

export type ClientEvent =
  | { type: "start"; scenario_id: string; autoplay?: boolean }
  | { type: "end" }
  | { type: "text"; text: string; source?: "typed" | "suggested" | "browser_stt" }
  | { type: "sentence_done"; id: string }
  | { type: "barge_in"; spoken_ids: string[] }
  | { type: "timing"; turn: number; vad_end_to_first_audio_ms: number };

// ---------------------------------------------------------------- HTTP

export interface ScenarioMeta {
  id: string;
  title: string;
  description: string;
  expected: "deal" | "no_deal" | "escalate" | string;
}
