/**
 * WebSocket protocol for `/ws/call/{call_id}?view=rep|operator`. GENERATED, do not edit.
 *
 * Source: src/types/events.schema.json, exported from app/schemas/events.py.
 * Regenerate with `npm run gen:types`. Hand-written helpers live in protocol.ts.
 *
 * Units: money is integer cents (`*_cents`, and `offer_total` on agreement),
 * percentages are integer basis points (`*_bp`, 4500 = 45%), dates are ISO
 * `YYYY-MM-DD` strings, timings are float milliseconds.
 */

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
  | TurnTraceEvent
  | AutoplayDoneEvent;
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
  | "ESCALATE"
  | "ANSWER";
export type Phase = "OPENING" | "DISCOVERY" | "NEGOTIATE" | "CONFIRM" | "WRAP" | "ESCALATE" | "END";
export type ClientEvent = StartEvent | EndEvent | TextEvent | SentenceDoneEvent | BargeInEvent | TimingEvent;
export type View = "rep" | "operator";

export interface WsProtocol {
  ServerEvent: ServerEvent;
  ClientEvent: ClientEvent;
  View: View;
}
export interface TranscriptEvent {
  blocked: boolean;
  role: "creditor" | "agent";
  sentence_id: string | null;
  spoken: boolean;
  text: string;
  type: "transcript";
}
export interface SayEvent {
  id: string;
  text: string;
  type: "say";
}
export interface BeliefEvent {
  terms: BeliefTerm[];
  type: "belief";
}
export interface BeliefTerm {
  evidence: Evidence[];
  field: string;
  history: (number | string | number[][] | null)[];
  status: "UNKNOWN" | "TENTATIVE" | "KNOWN" | "CONTRADICTED" | "ASSUMED";
  value: number | string | number[][] | null;
}
export interface Evidence {
  quote: string;
  turn: number;
}
export interface EvalEvent {
  additional_funds?: AdditionalFunds | null;
  agreed_bp: number | null;
  assumed_fields: string[];
  feasible: boolean;
  max_bp?: number | null;
  offer_total_cents: number | null;
  program_fee_cents?: number | null;
  rows: ScheduleRow[] | null;
  shape: string | null;
  type: "eval";
}
export interface AdditionalFunds {
  lump_sum: FundsOption;
  monthly_increment: FundsOption;
}
export interface FundsOption {
  amount_cents: number | null;
  date: string | null;
  num_drafts: number | null;
  reason: string | null;
  within_guardrail: boolean;
}
export interface ScheduleRow {
  balance_cents?: number | null;
  bank_fee_cents?: number | null;
  creditor_payment_cents: number;
  date: string;
  program_fee_cents?: number | null;
}
export interface BlockedEvent {
  offending?: string[] | null;
  reason: string;
  stage: "template" | "unfilled" | "rendered";
  type: "blocked";
}
export interface EscalateEvent {
  escalate_reason: string | null;
  reason: string | null;
  type: "escalate";
}
export interface LatencyEvent {
  engine_ms: number | null;
  nlg_ms: number | null;
  nlu_ms: number | null;
  policy_ms: number | null;
  queue_ms: number | null;
  server_total_ms: number | null;
  stt_ms: number | null;
  turn: number;
  type: "latency";
}
export interface AuditEvent {
  actor: string;
  event: string;
  id: number;
  payload: unknown;
  private?: boolean;
  ts: string;
  type: "audit";
}
export interface PhaseEvent {
  intent: Intent | null;
  phase: Phase;
  turn: number;
  type: "phase";
}
export interface AgreementEvent {
  assumed_fields: string[];
  bp: number;
  creditor: string;
  offer_total: number;
  rows: ScheduleRow[];
  status: "pending_client_approval";
  type: "agreement";
}
export interface SttErrorEvent {
  message: string;
  type: "stt_error";
}
export interface ErrorEvent {
  message: string;
  type: "error";
}
export interface TurnDoneEvent {
  type: "turn_done";
}
export interface TurnTraceEvent {
  affordability?: Affordability | null;
  ask_bp: number | null;
  ask_quote: string | null;
  belief_changes: TraceBeliefChange[];
  counter_bp: number | null;
  creditor_text: string | null;
  decide: Decide;
  dropped: DroppedTerm[];
  needs_info?: string[] | null;
  nlg: NlgTrace;
  notes?: TraceNote[] | null;
  reader?: TraceReader | null;
  spoken: SpokenSentence[];
  stance: ("offer" | "counter" | "accept" | "reject" | "stall" | "info" | "question" | "other") | null;
  terms: TraceTerm[];
  timings: {
    [k: string]: number | null;
  };
  turn: number;
  type: "turn_trace";
}
/**
 * PRIVATE engine view: highest feasible bp and the 1–100% curve. Operator only.
 */
export interface Affordability {
  curve: CurvePoint[];
  max_bp: number | null;
}
export interface CurvePoint {
  bp: number;
  feasible: boolean;
}
export interface TraceBeliefChange {
  field: string;
  new_status: "UNKNOWN" | "TENTATIVE" | "KNOWN" | "CONTRADICTED" | "ASSUMED";
  new_value: number | string | number[][] | null;
  old_status: "UNKNOWN" | "TENTATIVE" | "KNOWN" | "CONTRADICTED" | "ASSUMED";
  old_value: number | string | number[][] | null;
  quote?: string | null;
  turn: number;
}
/**
 * Policy move: raw ``reason`` code, its ``REASON_TEXT`` key and sentence.
 */
export interface Decide {
  intent: Intent;
  reason: string | null;
  reason_key: string;
  reason_short?: string | null;
  reason_text: string;
}
/**
 * A term ``post_verify`` dropped; ``reason`` is the audit event minus ``nlu_``.
 */
export interface DroppedTerm {
  field: string;
  quote?: string | null;
  reason: string;
  value?: unknown;
}
/**
 * How the line was phrased: mode, chosen template, guard verdicts, fallback.
 */
export interface NlgTrace {
  fallback_reason?: string | null;
  fallback_used: boolean;
  guards: GuardResult[];
  mode: "template" | "bank" | "llm";
  source: "default" | "override" | "bank" | "llm";
  template: string;
}
export interface GuardResult {
  offending?: string[] | null;
  ok: boolean;
  reason?: string | null;
  stage: "template" | "unfilled" | "rendered";
}
/**
 * A step of the turn worth telling in words, from its audit row (Phase 48).
 *
 * ``acked``: code acknowledged ``fields`` (and ``total`` cents) before the
 * move. ``ack_corrected`` / ``ack_disputed``: the rep corrected or disputed
 * an acked ``field`` (``old_value`` → ``new_value``). ``amount_held``: a dollar
 * amount (``cents``, ``quote``) was held back for the total-or-per-payment
 * question, ``trigger`` says why. ``*_dropped``: a pending question was
 * dropped, ``reason`` ``new_terms`` | ``interrupt``. ``amount_clarify_resolved``:
 * the rep answered it (``total`` or ``new_value`` = per-payment cents).
 */
export interface TraceNote {
  cents?: number | null;
  field?: string | null;
  fields?: string[] | null;
  kind:
    | "acked"
    | "ack_corrected"
    | "ack_disputed"
    | "cents_clarify_dropped"
    | "amount_held"
    | "amount_clarify_dropped"
    | "amount_clarify_resolved";
  new_value?: number | string | number[][] | null;
  old_value?: number | string | number[][] | null;
  quote?: string | null;
  reason?: string | null;
  total?: number | null;
  trigger?: string | null;
}
/**
 * Who read the rep's line this turn (Phase 48). Operator only, like the trace.
 *
 * ``kind``: ``llm`` (a model; ``provider`` / ``model`` name it), ``code`` (a
 * deterministic fast path such as a bare "Correct." to a read-back), or
 * ``script`` (oracle NLU: the simulated rep hands over its own reading).
 * ``fallback``: the answering model was not the route's first choice;
 * ``budget_reached``: a paid target was skipped for today's budget first.
 */
export interface TraceReader {
  budget_reached?: boolean;
  cache_hit?: boolean;
  fallback?: boolean;
  kind: "llm" | "code" | "script";
  model?: string | null;
  provider?: string | null;
}
export interface SpokenSentence {
  id: string;
  text: string;
}
/**
 * A term NLU extracted and ``post_verify`` kept; ``quote`` is a span of the rep line.
 */
export interface TraceTerm {
  field: string;
  hedged: boolean;
  quote: string;
  value: number | string | number[][] | null;
  verified: boolean;
}
export interface AutoplayDoneEvent {
  final_intent: Intent;
  outcome: "deal" | "no_deal" | "escalate" | "incomplete";
  phase: Phase;
  turns: number;
  type: "autoplay_done";
}
/**
 * Start a call. ``autoplay`` lets the sim creditor drive it (curated ids only).
 */
export interface StartEvent {
  autoplay?: boolean;
  autoplay_pause_ms?: number | null;
  scenario?: string | null;
  scenario_id?: string | null;
  scenario_payload?: {
    [k: string]: unknown;
  } | null;
  type: "start";
}
export interface EndEvent {
  type: "end";
}
export interface TextEvent {
  oracle?: {
    [k: string]: unknown;
  } | null;
  source?: string | null;
  text: string;
  type: "text";
}
export interface SentenceDoneEvent {
  id: string;
  type: "sentence_done";
}
export interface BargeInEvent {
  spoken_ids: string[];
  type: "barge_in";
}
export interface TimingEvent {
  turn?: number | null;
  type: "timing";
  vad_end_to_first_audio_ms?: number | null;
}
