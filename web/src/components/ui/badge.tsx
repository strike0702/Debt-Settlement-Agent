/** shadcn/ui Badge primitive. `tone` maps to the status tokens, never to chart series colours. */
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

const TONES = {
  neutral: "bg-surface-2 text-muted",
  accent: "bg-accent-soft text-accent",
  good: "bg-surface-2 text-good",
  bad: "bg-surface-2 text-bad",
  warn: "bg-surface-2 text-warn",
} as const;

export function Badge({
  tone = "neutral",
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: keyof typeof TONES }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        TONES[tone],
        className,
      )}
      {...props}
    />
  );
}
