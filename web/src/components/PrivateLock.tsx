/**
 * PRIVATE marking. `PrivateLock` replaces a whole panel in the creditor lens;
 * `PrivateTag` marks a private value the operator can see. One lock icon for both.
 */
import { Lock } from "lucide-react";
import { cn } from "@/lib/cn";

export function PrivateLock({ what, className }: { what: string; className?: string }) {
  return (
    <div
      role="note"
      data-testid="private-lock"
      className={cn(
        "flex items-center gap-3 rounded-lg border border-dashed border-border bg-surface-2 px-3 py-3 text-sm text-muted",
        className,
      )}
    >
      <Lock aria-hidden className="h-4 w-4 shrink-0" />
      <span>
        <span className="font-medium text-fg">{what}</span> is private to the firm and is not
        sent on the creditor's stream.
      </span>
    </div>
  );
}

export function PrivateTag({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs font-medium text-muted", className)}>
      <Lock aria-hidden className="h-3 w-3" />
      Private
    </span>
  );
}
