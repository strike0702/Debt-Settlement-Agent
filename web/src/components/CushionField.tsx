/**
 * "Minimum client cushion": a money field shown in the Debt negotiator view before a
 * call starts (next to Start call / Watch a call; custom cases too). Pre-filled with
 * the selected scenario's firm default; a changed value goes in that call's `start`
 * event only and is not stored. The engine keeps every balance at or above it when
 * some plan at the price can (a soft preference: it never changes which prices are
 * affordable). Client-protection policy, so the Creditor rep view never shows it.
 * The input draws no focus ring or outline; the caret alone shows focus. The help
 * text is a tooltip (hover or keyboard focus) and the input's `aria-describedby`; an
 * invalid amount shows its error in the same bubble until it is fixed.
 *
 * The typed text is accepted on Enter or blur when it parses (`onAccept`). The field's
 * border shows its state, with no extra text: green once the text is accepted (what the
 * call will use; also the initial firm default), the neutral resting grey while it is
 * being changed, red for an invalid entry (error in the tooltip). The green and red
 * tokens meet 3:1 against the page in both themes (light 4.7 / 5.2, dark 8.2 / 7.4).
 * Because color carries the state, a visually hidden polite live region says it too
 * ("Cushion set to $100.00 for this call" / "Invalid amount"), and errors set
 * `aria-invalid`.
 */
import { useId, useState } from "react";
import { money } from "@/lib/format";
import { parseCushion } from "@/lib/cushion";

/** The field's help text: shown as a tooltip and used as the input's accessible description. */
export const HINT = "Money the plan should leave in the client's account. This call only.";

export type CushionState = "accepted" | "editing" | "invalid";

const BORDER: Record<CushionState, string> = {
  accepted: "border-good",
  editing: "border-border",
  invalid: "border-bad",
};

export function CushionField({
  text,
  accepted,
  onText,
  onAccept,
}: {
  text: string;
  /** Last accepted value in cents (what the call will use). */
  accepted: number;
  onText: (text: string) => void;
  /** Called on Enter or blur with the parsed cents when the text is valid. */
  onAccept: (cents: number) => void;
}) {
  const id = useId();
  const parsed = parseCushion(text);
  const error = "error" in parsed ? parsed.error : null;
  // One bubble below the field: the hint on hover or keyboard focus, the error whenever
  // the amount is invalid. It overlays the page, so nothing sits under the field.
  const open = error ? "block" : "hidden group-hover:block group-focus-within:block";
  // Changed since the last accept. Starts false: the firm default is already accepted.
  const [dirty, setDirty] = useState(false);
  const state: CushionState = error ? "invalid" : dirty ? "editing" : "accepted";
  const accept = () => {
    if (!("cents" in parsed)) return;
    onAccept(parsed.cents);
    setDirty(false);
  };
  const status =
    state === "invalid"
      ? "Invalid amount"
      : state === "accepted"
        ? `Cushion set to ${money(accepted)} for this call`
        : "";
  return (
    <div className="group relative flex items-center gap-2" data-testid="cushion-field">
      <label htmlFor={id} className="text-sm text-muted">
        Minimum client cushion
      </label>
      <div
        className={`flex h-10 items-center rounded-lg border bg-bg px-3 ${BORDER[state]}`}
        data-state={state}
        data-testid="cushion-box"
      >
        <span aria-hidden className="text-muted">
          $
        </span>
        <input
          id={id}
          name="min-cushion"
          inputMode="decimal"
          autoComplete="off"
          spellCheck={false}
          value={text}
          onChange={(e) => {
            setDirty(true);
            onText(e.target.value);
          }}
          onBlur={accept}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              accept();
            }
          }}
          aria-invalid={error ? true : undefined}
          aria-describedby={`${id}-hint`}
          className="num w-20 border-0 bg-transparent pl-1 text-base shadow-none"
          // No focus ring or outline (user decision): the caret shows focus. Inline,
          // because the global unlayered `:focus-visible` outline beats utility classes.
          style={{ outline: "none" }}
        />
      </div>
      <span
        id={`${id}-hint`}
        role="tooltip"
        className={`${open} absolute top-full right-0 z-20 mt-1.5 w-max max-w-[min(18rem,calc(100vw-2rem))] rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs shadow-lg ${error ? "text-bad" : "text-fg"}`}
      >
        {error ?? HINT}
      </span>
      <span className="sr-only" aria-live="polite" data-testid="cushion-status">
        {status}
      </span>
    </div>
  );
}
