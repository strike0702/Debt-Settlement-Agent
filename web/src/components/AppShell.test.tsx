/**
 * Scenario cards on a phone: the selected card scrolls into its sideways row
 * (row `scrollLeft` only, never the page), on select and on first render.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "@/components/AppShell";
import { FIXTURE_SCENARIOS } from "@/fixtures";
import { revealInRow } from "@/lib/scrollRow";

const CARD = 240;
const GAP = 12;
const ROW = 390;

/** A 390 px row of 240 px cards 12 px apart, laid out against the row's scrollLeft. */
function fakeRow(row: HTMLElement): void {
  const items = [...row.children] as HTMLElement[];
  Object.defineProperty(row, "clientWidth", { configurable: true, value: ROW });
  Object.defineProperty(row, "scrollWidth", { configurable: true, value: 16 + items.length * (CARD + GAP) });
  row.getBoundingClientRect = () => ({ left: 0, right: ROW, top: 0, bottom: 100, width: ROW, height: 100, x: 0, y: 0, toJSON: () => ({}) });
  items.forEach((li, k) => {
    li.getBoundingClientRect = () => {
      const left = 16 + k * (CARD + GAP) - row.scrollLeft;
      return { left, right: left + CARD, top: 0, bottom: 100, width: CARD, height: 100, x: left, y: 0, toJSON: () => ({}) };
    };
  });
}

function rect(left: number, width: number): DOMRect {
  return { left, right: left + width, top: 0, bottom: 10, width, height: 10, x: left, y: 0, toJSON: () => ({}) };
}

describe("revealInRow", () => {
  function row(scrollWidth: number, clientWidth: number): HTMLElement {
    const el = document.createElement("ul");
    Object.defineProperty(el, "scrollWidth", { value: scrollWidth });
    Object.defineProperty(el, "clientWidth", { value: clientWidth });
    el.getBoundingClientRect = () => rect(0, clientWidth);
    return el;
  }
  const item = (left: number, width = CARD) => {
    const el = document.createElement("li");
    el.getBoundingClientRect = () => rect(left, width);
    return el;
  };

  it("scrolls right to show a card past the edge, then left to show one before it", () => {
    const r = row(1500, ROW);
    revealInRow(r, item(520));
    expect(r.scrollLeft).toBe(520 + CARD - ROW + 16);
    r.scrollLeft = 400;
    revealInRow(r, item(-100));
    expect(r.scrollLeft).toBe(400 - 100 - 16);
  });

  it("leaves a fully visible card and a row that does not scroll alone", () => {
    const r = row(1500, ROW);
    revealInRow(r, item(40));
    expect(r.scrollLeft).toBe(0);
    const wide = row(800, 1200);
    revealInRow(wide, item(900));
    expect(wide.scrollLeft).toBe(0);
  });
});

describe("scenario cards", () => {
  afterEach(() => vi.restoreAllMocks());
  const base = FIXTURE_SCENARIOS[0]!;
  const SCENARIOS = ["One", "Two", "Three", "Four", "Five"].map((n) => ({ ...base, id: `case_${n.toLowerCase()}`, title: `Case ${n}` }));

  function shell(selected: string) {
    return (
      <AppShell
        scenarios={SCENARIOS}
        selected={selected}
        onSelect={() => {}}
        onWatch={() => {}}
        watchLabel="Watch a call"
        playing={false}
        lens="operator"
        onLens={() => {}}
        theme="light"
        onTheme={() => {}}
        conversation={null}
        trace={null}
        state={null}
      />
    );
  }

  it("scrolls the selected card into the row without scrolling the page", () => {
    const pageScroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    const first = SCENARIOS[0]!.id;
    const last = SCENARIOS.at(-1)!;
    const { rerender } = render(shell(first));
    const row = screen.getByRole("button", { name: new RegExp(last.title) }).closest("ul")!;
    fakeRow(row);
    expect(row.scrollLeft).toBe(0);
    rerender(shell(last.id));
    const card = screen.getByRole("button", { name: new RegExp(last.title) }).closest("li")!;
    const box = card.getBoundingClientRect();
    expect(row.scrollLeft).toBeGreaterThan(0);
    expect(box.left).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(ROW);
    expect(pageScroll).not.toHaveBeenCalled();
  });
});
