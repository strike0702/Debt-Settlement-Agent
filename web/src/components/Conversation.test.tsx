/**
 * Suggested rep replies all look the same. A rep who haggles their
 * own way makes a "next" highlight wrong, so no suggestion is singled out; every
 * one stays clickable while the call is live. A line the script repeats is listed once.
 */
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Conversation } from "@/components/Conversation";
import type { ChatMessage } from "@/lib/callState";
import { SentenceReveal, wordTimes } from "@/lib/voice/reveal";

const UNIQUE = ["We're looking for seventy percent.", "Still seventy percent.", "Correct."];
const LINES = ["We're looking for seventy percent.", "Still seventy percent.", "Still seventy percent.", "Correct."];

function suggestionButtons(): HTMLElement[] {
  const heading = screen.getByText("Suggested rep replies");
  return within(heading.parentElement!).getAllByRole("button");
}

describe("suggested rep replies", () => {
  it("lists a repeated script line once, in first-occurrence order", () => {
    const script = ["A.", "B.", "A.", "C.", "B.", "B."];
    render(<Conversation messages={[]} suggested={script} emptyHint="" />);
    expect(suggestionButtons().map((b) => b.textContent)).toEqual(["A.", "B.", "C."]);
  });

  it("shows every suggestion in one neutral style, with no highlight", () => {
    render(<Conversation messages={[]} suggested={LINES} emptyHint="" onSend={() => {}} />);
    const buttons = suggestionButtons();
    expect(buttons.map((b) => b.textContent)).toEqual(UNIQUE);
    const styles = new Set(buttons.map((b) => b.className));
    expect(styles.size).toBe(1);
    for (const b of buttons) {
      expect(b.className).not.toContain("border-accent");
      expect(b.className).toContain("border-border");
    }
  });

  it("keeps every suggestion clickable when the call is live", () => {
    const onSend = vi.fn();
    render(<Conversation messages={[]} suggested={LINES} emptyHint="" onSend={onSend} />);
    const buttons = suggestionButtons();
    fireEvent.click(buttons[1]!);
    fireEvent.click(buttons[2]!);
    expect(onSend.mock.calls.map((c) => c[0])).toEqual(["Still seventy percent.", "Correct."]);
    for (const b of buttons) expect(b).toBeEnabled();
  });
});

describe("agent words in voice mode", () => {
  const agent = (id: string, text: string): ChatMessage => ({
    key: id,
    role: "agent",
    sentences: [{ id, text }],
    blocked: false,
  });

  it("draws no bubble until a word is spoken, then grows it", () => {
    vi.useFakeTimers();
    const reveal = new SentenceReveal({ now: () => Date.now(), reducedMotion: () => false });
    reveal.hold("a1", "Thank you.");
    render(<Conversation messages={[agent("a1", "Thank you.")]} suggested={[]} emptyHint="" reveal={reveal} />);
    expect(screen.queryByText("Agent")).toBeNull();
    act(() => reveal.start("a1", "Thank you."));
    act(() => void vi.advanceTimersByTime(wordTimes("Thank you.")[0]!));
    expect(screen.getByText("Thank")).toBeTruthy();
    act(() => reveal.end("a1"));
    expect(screen.getByText("Thank you.")).toBeTruthy();
    vi.useRealTimers();
  });
});
