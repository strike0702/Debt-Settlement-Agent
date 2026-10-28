/**
 * Horizontal card rows (the scenario picker below 1280 px) scroll sideways.
 * `revealInRow` brings one card fully into its row by moving the row's own
 * `scrollLeft` only; `scrollIntoView` would also scroll the page.
 */

/** Scroll `row` sideways so `item` is fully visible, aligned to `inset` px from the edge. */
export function revealInRow(row: HTMLElement, item: HTMLElement, inset = 16): void {
  if (row.scrollWidth <= row.clientWidth) return;
  const r = row.getBoundingClientRect();
  const i = item.getBoundingClientRect();
  if (i.left < r.left + inset) {
    row.scrollLeft += i.left - r.left - inset;
  } else if (i.right > r.right - inset) {
    // Align the start when the card is wider than the row; else its end.
    const byEnd = i.right - r.right + inset;
    const byStart = i.left - r.left - inset;
    row.scrollLeft += Math.min(byEnd, byStart);
  }
}
