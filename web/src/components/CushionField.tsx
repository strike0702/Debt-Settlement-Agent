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
 */
import { useId } from "react";
import { parseCushion } from "@/lib/cushion";

/** The field's help text: shown as a tooltip and used as the input's accessible description. */
export const HINT = "Money the plan should leave in the client's account. This call only.";

export function CushionField({
  text,
  onText,
}: {
  text: string;
  onText: (text: string) => void;
}) {
  const id = useId();
  const parsed = parseCushion(text);
  const error = "error" in parsed ? parsed.error : null;
  // One bubble below the field: the hint on hover or keyboard focus, the error whenever
  // the amount is invalid. It overlays the page, so nothing sits under the field.
  const open = error ? "block" : "hidden group-hover:block group-focus-within:block";
  return (
    <div className="group relative flex items-center gap-2" data-testid="cushion-field">
      <label htmlFor={id} className="text-sm text-muted">
        Minimum client cushion
      </label>
      <div className="flex h-10 items-center rounded-lg border border-border bg-bg px-3">
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
          onChange={(e) => onText(e.target.value)}
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
    </div>
  );
}
