/**
 * The reveal scheduler: words follow the estimated pace from the start event,
 * the end event catches up, a cut keeps only what played, and nothing stays
 * hidden when an event never comes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HOLD_MAX_MS, PACE, SentenceReveal, wordTimes } from "@/lib/voice/reveal";

const TEXT = "We can offer $362.50 today.";
let reduced: boolean;
let lags: number[];
let reveal: SentenceReveal;
const shown = (sid = "s1", text = TEXT) => reveal.visible(sid, text);

beforeEach(() => {
  vi.useFakeTimers();
  reduced = false;
  lags = [];
  reveal = new SentenceReveal({
    now: () => Date.now(),
    reducedMotion: () => reduced,
    onLag: (_sid, ms) => lags.push(ms),
  });
});
afterEach(() => vi.useRealTimers());

describe("wordTimes", () => {
  it("gives numbers far more time than their length", () => {
    const [plain, money] = [wordTimes("seven"), wordTimes("$712.50")];
    expect(money[0]! - PACE.sentence).toBe(3 * PACE.moneyDigit + PACE.cents);
    expect(money[0]!).toBeGreaterThan(5 * plain[0]!);
    expect(wordTimes("45%")[0]).toBe(PACE.sentence + PACE.percent);
    expect(wordTimes("2027")[0]).toBe(PACE.sentence + 4 * PACE.digit);
  });

  it("adds a comma's pause before the next word, not to the word itself", () => {
    const [a, b] = wordTimes("Okay, even");
    expect(a).toBe(PACE.sentence + 4 * PACE.letter);
    expect(b).toBe(a! + PACE.comma + 4 * PACE.letter);
  });
});

describe("reveal", () => {
  it("shows nothing before the start event and leaves unknown sentences whole", () => {
    reveal.hold("s1", TEXT);
    vi.advanceTimersByTime(HOLD_MAX_MS - 1);
    expect(shown()).toBe("");
    expect(reveal.revealing("s1")).toBe(true);
    expect(reveal.visible("other", "Opening line.")).toBe("Opening line.");
    expect(reveal.visible(null, "No id.")).toBe("No id.");
  });

  it("reveals words at the estimated pace and never past the sentence", () => {
    const times = wordTimes(TEXT);
    reveal.hold("s1", TEXT);
    reveal.start("s1", TEXT);
    expect(shown()).toBe("");
    vi.advanceTimersByTime(times[0]!);
    expect(shown()).toBe("We");
    vi.advanceTimersByTime(times[2]! - times[0]!);
    expect(shown()).toBe("We can offer");
    vi.advanceTimersByTime(times[3]! - times[2]! - 1);
    expect(shown()).toBe("We can offer");
    vi.advanceTimersByTime(1);
    expect(shown()).toBe("We can offer $362.50");
    vi.advanceTimersByTime(60_000);
    expect(shown()).toBe(TEXT);
  });

  it("shows the whole sentence on the end event and reports the lag", () => {
    reveal.hold("s1", TEXT);
    reveal.start("s1", TEXT);
    vi.advanceTimersByTime(1000);
    reveal.end("s1");
    expect(shown()).toBe(TEXT);
    expect(reveal.revealing("s1")).toBe(false);
    expect(lags).toEqual([1000 - wordTimes(TEXT).at(-1)!]);
  });

  it("keeps only the words of the audio that played on a cut, whatever the pace showed", () => {
    const times = wordTimes(TEXT);
    reveal.hold("s0", "First sentence.");
    reveal.hold("s1", TEXT);
    reveal.hold("s2", "Never said.");
    reveal.start("s0", "First sentence.");
    reveal.end("s0");
    reveal.start("s1", TEXT);
    vi.advanceTimersByTime(times[3]!); // the pace ran on while the agent was paused
    expect(shown()).toBe("We can offer $362.50");
    reveal.cut("s1", ["s0"], ["s1", "s2"], times[1]! + 1);
    expect(reveal.visible("s0", "First sentence.")).toBe("First sentence.");
    expect(shown()).toBe("We can…");
    expect(reveal.visible("s2", "Never said.")).toBe("");
    vi.advanceTimersByTime(60_000);
    reveal.release();
    expect(shown()).toBe("We can…");
    expect(reveal.visible("s2", "Never said.")).toBe("");
  });

  it("drops the comma a cut stopped on", () => {
    const text = "Okay, even payments.";
    reveal.hold("s1", text);
    reveal.cut("s1", [], ["s1"], wordTimes(text)[0]!);
    expect(reveal.visible("s1", text)).toBe("Okay…");
  });

  it("shows nothing of a sentence cut before any word finished", () => {
    reveal.hold("s1", TEXT);
    reveal.cut("s1", [], ["s1"], 0);
    expect(shown()).toBe("");
  });

  it("with reduced motion shows the sentence whole at its start, not before", () => {
    reduced = true;
    reveal.hold("s1", TEXT);
    expect(shown()).toBe("");
    reveal.start("s1", TEXT);
    expect(shown()).toBe(TEXT);
  });

  it("notifies on every visible change", () => {
    const fn = vi.fn();
    reveal.subscribe(fn);
    reveal.hold("s1", "Two words.");
    reveal.start("s1", "Two words.");
    const before = fn.mock.calls.length;
    vi.advanceTimersByTime(60_000);
    expect(fn.mock.calls.length).toBe(before + 2);
  });
});

describe("a missed event never leaves text hidden", () => {
  it("the rep's next turn shows what never started", () => {
    reveal.hold("s1", TEXT);
    reveal.release();
    expect(shown()).toBe(TEXT);
    reveal.start("s1", TEXT); // a late start changes nothing
    expect(shown()).toBe(TEXT);
  });

  it("a later sentence's start shows the one before it", () => {
    reveal.hold("s1", TEXT);
    reveal.hold("s2", "Next one.");
    reveal.start("s2", "Next one.");
    expect(shown()).toBe(TEXT);
    expect(reveal.visible("s2", "Next one.")).toBe("");
  });

  it("a sentence playing with no end event finishes at its own pace", () => {
    reveal.hold("s1", TEXT);
    reveal.start("s1", TEXT);
    reveal.release();
    expect(shown()).toBe(TEXT);
  });

  it("quiet for too long shows what is waiting, but not while a sentence still plays", () => {
    const long = Array(40).fill("settlement").join(" ");
    reveal.hold("s1", long);
    reveal.hold("s2", TEXT);
    reveal.start("s1", long);
    vi.advanceTimersByTime(HOLD_MAX_MS + 1);
    expect(reveal.visible("s2", TEXT)).toBe("");
    reveal.end("s1");
    vi.advanceTimersByTime(HOLD_MAX_MS);
    expect(reveal.visible("s2", TEXT)).toBe(TEXT);
  });

  it("speech_dropped hides what was never played, even after a release", () => {
    reveal.hold("s1", TEXT);
    reveal.release();
    reveal.drop(["s1"]);
    expect(shown()).toBe("");
  });
});
