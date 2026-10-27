/**
 * "Minimum client cushion": a money field shown in the Debt negotiator view before a
 * call starts (next to Start call / Watch a call; custom cases too). Pre-filled with
 * the selected scenario's firm default; a changed value goes in that call's `start`
 * event only and is not stored. The engine keeps every balance at or above it when
 * some plan at the price can (a soft preference: it never changes which prices are
 * affordable). Client-protection policy, so the Creditor rep view never shows it.
 */
import { useId } from "react";
import { parseCushion } from "@/lib/cushion";

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
  return (
    <div className="flex flex-col gap-1" data-testid="cushion-field">
      <div className="flex items-center gap-2">
        <label htmlFor={id} className="text-sm text-muted">
          Minimum client cushion
        </label>
        <div className="flex h-10 items-center rounded-lg border border-border bg-bg px-3 focus-within:ring-2 focus-within:ring-accent">
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
            className="num w-20 bg-transparent pl-1 text-base outline-none"
          />
        </div>
      </div>
      <p id={`${id}-hint`} className={error ? "text-xs text-bad" : "text-xs text-muted"}>
        {error ?? "Money the plan should leave in the client's account. This call only."}
      </p>
    </div>
  );
}
