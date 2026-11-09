/**
 * Voice mode: shows the agent's words about as they are spoken.
 *
 * Aura-2 gives no word timestamps, so the pace is an estimate (DECISIONS D25).
 * The server says when a sentence's first audio played (`agent_sentence_start`),
 * when its last audio played (`agent_sentence_end`) and when the agent was
 * stopped (`agent_sentence_cut`); between start and end this class reveals the
 * sentence one word at a time from the table below. The end event shows the
 * whole sentence at once. A cut keeps only the words that fit in the audio that
 * really played (`heard_ms`, from the server), so a pace running ahead of a
 * paused agent cannot leave unspoken words on screen.
 *
 * The rule for failures: text is never left hidden. A sentence whose start never
 * came is shown whole when a later sentence starts, when the rep's next line
 * arrives or voice goes off (`release`), or after `HOLD_MAX_MS` with nothing
 * happening. Only what the server says was dropped stays hidden.
 *
 * Not here: the transcript itself (`callState`), which always keeps every line.
 * Sentences this class was never told to hold (text mode, fixtures, an opening
 * shown before voice was turned on) are shown whole. With reduced motion a
 * sentence appears whole at its start. `WebRtcVoice` feeds it; `Conversation`
 * reads it.
 */

/**
 * Milliseconds of Aura-2 audio per token part, least squares over 30 demo
 * sentences in the agent's voice (aura-2-thalia-en). Measured with
 * `docs/eval/voice_reveal_20261109/`: median error about 360 ms on a held-out
 * sentence. Numbers take far longer than their length: "$712.50" is "seven
 * hundred twelve dollars and fifty cents".
 */
export const PACE = {
  /** Each letter of a plain word. */
  letter: 58,
  /** Each digit of a dollar amount, before the cents. */
  moneyDigit: 520,
  /** A dollar amount's ".50". */
  cents: 1200,
  /** One percentage, "45%". */
  percent: 577,
  /** Each digit of any other number (dates, counts, years). */
  digit: 265,
  /** The pause after a comma, colon or semicolon. */
  comma: 530,
  /** Once per sentence, before its first word. */
  sentence: 205,
} as const;

/** With sentences waiting and nothing happening for this long, show them. */
export const HOLD_MAX_MS = 5000;

function speakMs(token: string): number {
  const bare = token.replace(/[.,?!:;]+$/, "");
  const digits = (s: string) => (s.match(/\d/g) ?? []).length;
  if (bare.startsWith("$")) {
    const [dollars = "", cents] = bare.split(".");
    return digits(dollars) * PACE.moneyDigit + (cents ? PACE.cents : 0);
  }
  if (bare.endsWith("%")) return PACE.percent;
  if (/\d/.test(bare)) return digits(bare) * PACE.digit;
  return (bare.match(/\p{L}/gu) ?? []).length * PACE.letter;
}

/** When each word's estimated audio ends, in ms after the sentence's first audio. */
export function wordTimes(text: string): number[] {
  let t: number = PACE.sentence;
  return words(text).map((token) => {
    t += speakMs(token);
    const at = t;
    if (/[,:;]$/.test(token)) t += PACE.comma;
    return at;
  });
}

function words(text: string): string[] {
  return text.split(/\s+/).filter(Boolean);
}

type Phase = "held" | "playing" | "done" | "cut" | "dropped";

interface Entry {
  phase: Phase;
  words: string[];
  /** Words shown (`playing` and `cut`). */
  shown: number;
  times: number[];
  startedAt: number;
}

type Timer = ReturnType<typeof setTimeout>;

export interface RevealDeps {
  now(): number;
  reducedMotion(): boolean;
  /** How far the last word was from the end event (positive: the text was early). */
  onLag?(sid: string, lagMs: number): void;
}

export class SentenceReveal {
  private entries = new Map<string, Entry>();
  private version = 0;
  private listeners = new Set<() => void>();
  private wordTimer: Timer | null = null;
  private holdTimer: Timer | null = null;
  private deps: RevealDeps;

  constructor(deps: RevealDeps) {
    this.deps = deps;
  }

  // ------------------------------------------------------------ store

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** Changes whenever any visible text changed. */
  getVersion = (): number => this.version;

  /** What to show of a sentence; a sentence this class does not hold shows whole. */
  visible(sid: string | null, text: string): string {
    const e = sid == null ? undefined : this.entries.get(sid);
    if (!e || e.phase === "done") return text;
    if (e.phase === "held" || e.phase === "dropped") return "";
    const part = e.words.slice(0, e.shown).join(" ");
    if (e.phase === "playing") return e.shown === e.words.length ? text : part;
    // Cut off: drop the punctuation it stopped on, so "balance,…" reads "balance…".
    return part && `${part.replace(/[.,?!:;]+$/, "")}…`;
  }

  /** The sentence may still grow (screen readers wait for the whole bubble). */
  revealing(sid: string | null): boolean {
    const e = sid == null ? undefined : this.entries.get(sid);
    return e?.phase === "held" || (e?.phase === "playing" && e.shown < e.words.length);
  }

  private changed(): void {
    this.version += 1;
    this.listeners.forEach((fn) => fn());
  }

  // ------------------------------------------------------------ events

  /** Voice mode: a new agent sentence is decided; show nothing until it is spoken. */
  hold(sid: string, text: string): void {
    if (this.entries.has(sid)) return;
    this.entries.set(sid, { phase: "held", words: words(text), shown: 0, times: [], startedAt: 0 });
    this.armHold();
    this.changed();
  }

  /** The sentence's first audio played. Anything before it can no longer wait. */
  start(sid: string, text: string): void {
    const e = this.entries.get(sid);
    if (e?.phase !== "held") return;
    for (const [id, other] of this.entries) {
      if (id === sid) break;
      if (other.phase === "held" || other.phase === "playing") other.phase = "done";
    }
    e.phase = "playing";
    e.words = words(text);
    e.times = wordTimes(text);
    e.startedAt = this.deps.now();
    e.shown = this.deps.reducedMotion() ? e.words.length : 0;
    this.scheduleWord(sid, e);
    this.armHold();
    this.changed();
  }

  /** The sentence's last audio played: show all of it (catch-up). */
  end(sid: string): void {
    const e = this.entries.get(sid);
    if (e?.phase === "playing") {
      const last = e.times.at(-1);
      if (last != null) this.deps.onLag?.(sid, Math.round(this.deps.now() - e.startedAt - last));
    }
    if (!e || e.phase === "cut" || e.phase === "dropped" || e.phase === "done") return;
    e.phase = "done";
    this.armHold();
    this.changed();
  }

  /** The agent was stopped: `sid` keeps the words of its `heardMs` of audio, `dropped` go. */
  cut(sid: string, heard: readonly string[], dropped: readonly string[], heardMs: number): void {
    for (const id of heard) {
      const e = this.entries.get(id);
      if (e && e.phase !== "dropped") e.phase = "done";
    }
    for (const id of dropped) {
      const e = this.entries.get(id);
      if (e) e.phase = "dropped";
    }
    const e = this.entries.get(sid);
    if (e) {
      const times = e.times.length ? e.times : wordTimes(e.words.join(" "));
      e.phase = "cut";
      e.shown = times.filter((t) => t <= heardMs).length;
    }
    this.armHold();
    this.changed();
  }

  /** The server dropped these unspoken sentences (`speech_dropped`). */
  drop(ids: readonly string[]): void {
    let any = false;
    for (const id of ids) {
      const e = this.entries.get(id);
      // A sentence playing gets its own cut, which knows how much of it played.
      if (e && (e.phase === "held" || e.phase === "done")) {
        e.phase = "dropped";
        any = true;
      }
    }
    if (any) {
      this.armHold();
      this.changed();
    }
  }

  /** The turn is over (rep's next line, voice off): nothing may stay hidden. */
  release(): void {
    let any = false;
    for (const e of this.entries.values()) {
      if (e.phase === "held" || e.phase === "playing") {
        e.phase = "done";
        any = true;
      }
    }
    this.clearTimers();
    if (any) this.changed();
  }

  /** New call. */
  clear(): void {
    this.clearTimers();
    if (this.entries.size === 0) return;
    this.entries.clear();
    this.changed();
  }

  // ------------------------------------------------------------ timers

  private scheduleWord(sid: string, e: Entry): void {
    if (this.wordTimer != null) clearTimeout(this.wordTimer);
    this.wordTimer = null;
    if (e.phase !== "playing" || e.shown >= e.words.length) return;
    const due = e.startedAt + (e.times[e.shown] ?? 0) - this.deps.now();
    this.wordTimer = setTimeout(() => this.tick(sid), Math.max(0, due));
  }

  private tick(sid: string): void {
    this.wordTimer = null;
    const e = this.entries.get(sid);
    if (e?.phase !== "playing") return;
    // From the clock, not a count of ticks: a throttled background tab catches up.
    const elapsed = this.deps.now() - e.startedAt;
    const shown = e.times.filter((t) => t <= elapsed).length;
    if (shown !== e.shown) {
      e.shown = shown;
      this.changed();
    }
    this.scheduleWord(sid, e);
  }

  private armHold(): void {
    if (this.holdTimer != null) clearTimeout(this.holdTimer);
    this.holdTimer = null;
    const waiting = [...this.entries.values()].some((e) => e.phase === "held");
    if (waiting) this.holdTimer = setTimeout(() => this.holdExpired(), HOLD_MAX_MS);
  }

  private holdExpired(): void {
    this.holdTimer = null;
    const speaking = [...this.entries.values()].some((e) => e.phase === "playing" && e.shown < e.words.length);
    if (speaking) {
      this.armHold(); // a long sentence is still going: the next one is only waiting its turn
      return;
    }
    let any = false;
    for (const e of this.entries.values()) {
      if (e.phase === "held") {
        e.phase = "done";
        any = true;
      }
    }
    if (any) this.changed();
  }

  private clearTimers(): void {
    if (this.wordTimer != null) clearTimeout(this.wordTimer);
    if (this.holdTimer != null) clearTimeout(this.holdTimer);
    this.wordTimer = this.holdTimer = null;
  }
}
