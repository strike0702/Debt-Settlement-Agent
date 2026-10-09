/**
 * Phase 50b: suggested rep replies all look the same. A rep who haggles their
 * own way makes a "next" highlight wrong, so no suggestion is singled out; every
 * one stays clickable while the call is live, repeated lines included.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Conversation } from "@/components/Conversation";

const LINES = ["We're looking for seventy percent.", "Still seventy percent.", "Still seventy percent.", "Correct."];

function suggestionButtons(): HTMLElement[] {
  const heading = screen.getByText("Suggested rep replies");
  return within(heading.parentElement!).getAllByRole("button");
}

describe("suggested rep replies", () => {
  it("shows every suggestion in one neutral style, with no highlight", () => {
    render(<Conversation messages={[]} mic="off" suggested={LINES} emptyHint="" onSend={() => {}} />);
    const buttons = suggestionButtons();
    expect(buttons.map((b) => b.textContent)).toEqual(LINES);
    const styles = new Set(buttons.map((b) => b.className));
    expect(styles.size).toBe(1);
    for (const b of buttons) {
      expect(b.className).not.toContain("border-accent");
      expect(b.className).toContain("border-border");
    }
  });

  it("keeps every suggestion clickable when the call is live", () => {
    const onSend = vi.fn();
    render(<Conversation messages={[]} mic="off" suggested={LINES} emptyHint="" onSend={onSend} />);
    const buttons = suggestionButtons();
    fireEvent.click(buttons[2]!);
    fireEvent.click(buttons[3]!);
    expect(onSend.mock.calls.map((c) => c[0])).toEqual(["Still seventy percent.", "Correct."]);
    for (const b of buttons) expect(b).toBeEnabled();
  });
});
