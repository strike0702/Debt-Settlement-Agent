/**
 * Folds the server event stream into one view model for the call console.
 *
 * Pure and framework-free: the fixture replay and the live `useCall` hook both
 * feed events through `reduceCall`. Besides server events it takes one local
 * event, `tts_onset` (from `useVoice`: `say` received → speech audibly starts),
 * which becomes the `tts_onset_ms` stage of that turn's latency waterfall.
 * It does not filter private data; `toRepView` in `repView.ts` does that
 * before events get here.
 */
import type { CallError } from "@/lib/callError";
import type {
  AgreementEvent,
  AuditEvent,
  AutoplayDoneEvent,
  BeliefTerm,
  BlockedEvent,
  EscalateEvent,
  EvalEvent,
  Intent,
  LatencyEvent,
  OperatorDetail,
  Phase,
  ServerEvent,
  TurnTraceEvent,
} from "@/types/protocol";

/** Client-measured: ms from the turn's first `say` to `SpeechSynthesisUtterance.onstart`. */
export interface TtsOnsetEvent {
  type: "tts_onset";
  turn: number;
  ms: number;
}

export type CallEvent = ServerEvent | TtsOnsetEvent;

export interface ChatMessage {
  key: string;
  role: "creditor" | "agent";
  sentences: { id: string | null; text: string }[];
  blocked: boolean;
}

export interface CallState {
  messages: ChatMessage[];
  traces: TurnTraceEvent[];
  belief: BeliefTerm[];
  evaluation: EvalEvent | null;
  latency: LatencyEvent[];
  audit: AuditEvent[];
  blocked: BlockedEvent[];
  phase: Phase | null;
  intent: Intent | null;
  turn: number;
  agreement: AgreementEvent | null;
  escalation: EscalateEvent | null;
  /** Set by the last frame of an autoplayed call. */
  autoplay: AutoplayDoneEvent | null;
  /** TTS onsets that arrived before their turn's trace. */
  ttsOnset: Record<number, number>;
  /** Newest server `error` / `stt_error` of this call; cleared by the next agent reply. */
  error: CallError | null;
  /** Errors seen in this call so far (gives each `error` its `seq`). */
  errorCount: number;
  /** Rep line received, agent reply not yet out. */
  thinking: boolean;
  /** Id of the agent sentence most recently sent for TTS. */
  speakingId: string | null;
}

export const initialCallState: CallState = {
  messages: [],
  traces: [],
  belief: [],
  evaluation: null,
  latency: [],
  audit: [],
  blocked: [],
  phase: null,
  intent: null,
  turn: 0,
  agreement: null,
  escalation: null,
  autoplay: null,
  ttsOnset: {},
  error: null,
  errorCount: 0,
  thinking: false,
  speakingId: null,
};

function withOnset(t: TurnTraceEvent, ms: number | undefined): TurnTraceEvent {
  return ms == null ? t : { ...t, timings: { ...t.timings, tts_onset_ms: ms } };
}

/** Apply one event. Never mutates `state`. */
export function reduceCall(state: CallState, ev: CallEvent): CallState {
  switch (ev.type) {
    case "transcript": {
      const last = state.messages.at(-1);
      const sentence = { id: ev.sentence_id, text: ev.text };
      // Consecutive sentences from one speaker form one bubble.
      if (last && last.role === ev.role) {
        const merged: ChatMessage = {
          ...last,
          sentences: [...last.sentences, sentence],
          blocked: last.blocked || ev.blocked,
        };
        return { ...state, messages: [...state.messages.slice(0, -1), merged] };
      }
      const msg: ChatMessage = {
        key: `${state.messages.length}-${ev.role}`,
        role: ev.role,
        sentences: [sentence],
        blocked: ev.blocked,
      };
      return {
        ...state,
        messages: [...state.messages, msg],
        thinking: ev.role === "creditor" ? true : state.thinking,
        // The agent answered (both views get its transcript), so the last error is over.
        error: ev.role === "agent" ? null : state.error,
      };
    }
    case "say":
      return { ...state, thinking: false, speakingId: ev.id, error: null };
    case "belief":
      return { ...state, belief: ev.terms };
    case "eval":
      return { ...state, evaluation: ev };
    case "phase":
      return {
        ...state,
        phase: ev.phase,
        intent: ev.intent ?? state.intent,
        turn: ev.turn,
      };
    case "blocked":
      return { ...state, blocked: [...state.blocked, ev] };
    case "escalate":
      return { ...state, escalation: ev };
    case "latency":
      return {
        ...state,
        latency: [...state.latency.filter((l) => l.turn !== ev.turn), ev],
      };
    case "audit":
      if (state.audit.some((a) => a.id === ev.id)) return state;
      return { ...state, audit: [...state.audit, ev] };
    case "agreement":
      return { ...state, agreement: ev };
    case "turn_trace":
      return {
        ...state,
        traces: [...state.traces.filter((t) => t.turn !== ev.turn), withOnset(ev, state.ttsOnset[ev.turn])],
      };
    case "tts_onset":
      if (state.traces.some((t) => t.turn === ev.turn)) {
        return {
          ...state,
          traces: state.traces.map((t) => (t.turn === ev.turn ? withOnset(t, ev.ms) : t)),
        };
      }
      return { ...state, ttsOnset: { ...state.ttsOnset, [ev.turn]: ev.ms } };
    case "autoplay_done":
      return { ...state, autoplay: ev, thinking: false };
    case "stt_error":
    case "error":
      return {
        ...state,
        thinking: false,
        error: { kind: ev.type, message: ev.message, seq: state.errorCount + 1 },
        errorCount: state.errorCount + 1,
      };
    case "turn_done":
      return state;
    case "speech_dropped":
      // Voice-only: the engine stops those sentences; the transcript keeps the lines.
      return state;
    case "voice_state":
      // Pipecat voice only: the WebRTC client shows it; the transcript is unchanged.
      return state;
  }
}

export function foldCall(events: readonly CallEvent[]): CallState {
  return events.reduce(reduceCall, initialCallState);
}

/**
 * Lay the server's Debt negotiator detail (`GET /calls/{id}/operator`) over a
 * call folded from a rep stream: its traces (with any local TTS
 * onsets), the full audit log with privacy flags, and the eval and agreement
 * with their fee columns. Everything else (conversation, belief, phase) stays
 * from the live stream, which is always at least as new.
 */
export function withOperatorDetail(state: CallState, detail: OperatorDetail): CallState {
  const traces = detail.traces.map((t) => withOnset(t, state.ttsOnset[t.turn]));
  const known = new Set(detail.audit.map((a) => a.id));
  return {
    ...state,
    traces,
    audit: [...detail.audit, ...state.audit.filter((a) => !known.has(a.id))],
    evaluation: detail.eval ?? state.evaluation,
    agreement: detail.agreement ?? state.agreement,
  };
}

export interface LadderPoint {
  turn: number;
  ask: number | null;
  counter: number | null;
}

/** Ask and counter (bp) per turn that had either, for the negotiation ladder. */
export function ladderPoints(traces: readonly TurnTraceEvent[]): LadderPoint[] {
  return traces
    .filter((t) => t.ask_bp != null || t.counter_bp != null)
    .map((t) => ({ turn: t.turn, ask: t.ask_bp, counter: t.counter_bp }));
}

/** A call is over once it ends as a deal (END) or a handoff (ESCALATE phase or event). */
export function isCallOver(state: CallState): boolean {
  return state.phase === "END" || state.phase === "ESCALATE" || state.escalation != null;
}
