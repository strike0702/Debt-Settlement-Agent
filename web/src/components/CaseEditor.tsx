/**
 * "Add a test case" editor (Phase 36): a JSON textarea pre-filled with
 * `GET /scenarios/template`, checked against `POST /scenarios/preview` as the
 * visitor types, with every problem shown next to the editor by field path.
 *
 * It sits inline under the scenario cards rather than in a modal, so the
 * cards the case will join stay in view and phones get the full width. It
 * does not store anything: App owns the list of custom cases (and their
 * `localStorage` copy); this only hands back a validated payload and the rep
 * card's suggested replies (`POST /scenarios/preview/rep`).
 *
 * Keyboard: Tab indents (two spaces). Esc, then Tab, leaves the editor, so the
 * textarea is never a keyboard trap.
 */
import { AlertCircle, CheckCircle2, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/cn";
import { type FieldError, locatePath, parseCase } from "@/lib/customCases";
import type { PreviewErrors, RepPreview } from "@/types/protocol";

export interface CaseEditorProps {
  mode: "add" | "edit";
  /** The JSON to start from (the template, or the case being edited). */
  initialText: string;
  /** Pretty-printed template for "Reset to template"; null while it loads. */
  templateText: string | null;
  onSave: (payload: Record<string, unknown>, suggested: string[]) => void;
  onCancel: () => void;
}

type Check =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "ok" }
  | { status: "invalid"; errors: (FieldError & { line: number | null })[] }
  | { status: "offline"; message: string };

const CHECK_DELAY_MS = 600;
const INDENT = "  ";

function lineOf(text: string, offset: number | null): number | null {
  return offset == null ? null : text.slice(0, offset).split("\n").length;
}

/** Validate locally (JSON syntax) and then on the server (fields); never throws. */
export async function checkCase(text: string, signal?: AbortSignal): Promise<Check> {
  const parsed = parseCase(text);
  if (!parsed.ok) return { status: "invalid", errors: [parsed.error] };
  try {
    const res = await fetch("/scenarios/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed.payload),
      signal,
    });
    if (res.ok) return { status: "ok" };
    const body = (await res.json().catch(() => null)) as PreviewErrors | null;
    const detail = body?.detail;
    const errors =
      detail && typeof detail === "object" && Array.isArray(detail.errors) && detail.errors.length > 0
        ? detail.errors
        : [{ path: "", message: typeof detail === "string" ? detail : `The server rejected this case (HTTP ${res.status}).` }];
    return { status: "invalid", errors: errors.map((e) => ({ ...e, line: lineOf(text, locatePath(text, e.path)) })) };
  } catch (e) {
    if (signal?.aborted) return { status: "checking" };
    return { status: "offline", message: `Could not reach the server to check this case (${String(e)}).` };
  }
}

export function CaseEditor({ mode, initialText, templateText, onSave, onCancel }: CaseEditorProps) {
  const [text, setText] = useState(initialText);
  const [check, setCheck] = useState<Check>({ status: "idle" });
  const [saving, setSaving] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const escArmed = useRef(false);
  const headingId = useId();
  const helpId = useId();
  const statusId = useId();

  // Check a moment after typing stops; a newer edit cancels the older request.
  useEffect(() => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      setCheck({ status: "checking" });
      void checkCase(text, ctrl.signal).then((c) => {
        if (!ctrl.signal.aborted) setCheck(c);
      });
    }, CHECK_DELAY_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [text]);

  const lines = text.split("\n").length;
  const errorLines = new Set(check.status === "invalid" ? check.errors.map((e) => e.line).filter((l) => l != null) : []);

  const focusAt = (offset: number | null) => {
    const el = area.current;
    if (!el || offset == null) return;
    el.focus();
    el.setSelectionRange(offset, offset);
    // Bring the line into view: ~1.5 rem per line.
    const line = lineOf(text, offset) ?? 1;
    el.scrollTop = Math.max(0, (line - 4) * 21);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Escape") {
      escArmed.current = true;
      return;
    }
    if (e.key === "Tab" && !escArmed.current && !e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      const el = e.currentTarget;
      // execCommand keeps the browser's undo stack; fall back to a plain edit.
      if (!document.execCommand?.("insertText", false, INDENT)) {
        const { selectionStart: a, selectionEnd: b } = el;
        const next = text.slice(0, a) + INDENT + text.slice(b);
        setText(next);
        requestAnimationFrame(() => el.setSelectionRange(a + INDENT.length, a + INDENT.length));
      }
    }
    escArmed.current = false;
  };

  const save = async () => {
    setSaving(true);
    const c = await checkCase(text);
    setCheck(c);
    if (c.status !== "ok") {
      setSaving(false);
      if (c.status === "invalid") {
        const first = c.errors[0]!;
        focusAt(first.path ? locatePath(text, first.path) : null);
      }
      return;
    }
    const parsed = parseCase(text);
    if (!parsed.ok) return setSaving(false);
    let suggested: string[] = [];
    try {
      const res = await fetch("/scenarios/preview/rep", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed.payload),
      });
      if (res.ok) suggested = ((await res.json()) as RepPreview).suggested ?? [];
    } catch {
      // Suggested replies are a convenience; the case still works without them.
    }
    setSaving(false);
    onSave(parsed.payload, suggested);
  };

  const title = mode === "add" ? "Add a test case" : "Edit test case";
  return (
    <Card>
      <section aria-labelledby={headingId} className="flex flex-col gap-4 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id={headingId} className="text-base font-semibold">
              {title}
            </h2>
            <p className="mt-1 max-w-[70ch] text-sm text-pretty text-muted">
              A case is one JSON object: the creditor's balances (<code className="font-mono text-fg">offer</code>), the
              firm's fees (<code className="font-mono text-fg">firm</code>), the client's savings plan (
              <code className="font-mono text-fg">client</code>) and the card you read when you play the rep (
              <code className="font-mono text-fg">rep_card</code>). Money is in cents, so 22000 is $220.00. Dates
              are YYYY-MM-DD.
            </p>
          </div>
          <Button size="icon" variant="ghost" aria-label="Close the editor" onClick={onCancel}>
            <X aria-hidden className="h-4 w-4" />
          </Button>
        </div>

        <div className="grid grid-cols-1 gap-4 mid:grid-cols-[minmax(0,1fr)_minmax(16rem,20rem)]">
          <div className="flex min-w-0 flex-col gap-1.5">
            <label htmlFor={`${headingId}-json`} className="text-sm font-medium">
              Test case JSON
            </label>
            <div className="flex h-[26rem] overflow-hidden rounded-lg border border-border bg-bg focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/40">
              <div
                ref={gutter}
                aria-hidden
                className="shrink-0 overflow-hidden border-r border-border bg-surface-2 py-2 text-right font-mono text-xs leading-[21px] text-faint select-none num"
              >
                {Array.from({ length: lines }, (_, i) => (
                  <div key={i} className={cn("px-2", errorLines.has(i + 1) && "font-semibold text-bad")}>
                    {i + 1}
                  </div>
                ))}
              </div>
              <textarea
                id={`${headingId}-json`}
                ref={area}
                name="test-case-json"
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={onKeyDown}
                onScroll={(e) => {
                  if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop;
                }}
                spellCheck={false}
                autoComplete="off"
                autoCapitalize="off"
                autoCorrect="off"
                wrap="off"
                aria-describedby={`${helpId} ${statusId}`}
                aria-invalid={check.status === "invalid"}
                className="min-w-0 flex-1 resize-none bg-transparent px-3 py-2 font-mono text-xs leading-[21px] text-fg outline-none focus-visible:outline-none"
              />
            </div>
            <p id={helpId} className="text-xs text-muted">
              Tab indents. Press Esc, then Tab, to leave the editor.
            </p>
          </div>

          <div className="flex min-w-0 flex-col gap-3">
            <div id={statusId} aria-live="polite" className="flex flex-col gap-2 text-sm">
              <CheckStatus check={check} onJump={(path) => focusAt(locatePath(text, path))} />
            </div>
            <div className="rounded-lg bg-surface-2 p-3 text-sm text-muted">
              <p className="font-medium text-fg">You play the rep on custom cases</p>
              <p className="mt-1">
                The simulated rep only knows the built-in cases. Select your case, press Start call, and reply as the
                creditor by typing, clicking a suggested reply, or using the mic.
              </p>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={() => void save()} disabled={saving}>
            {saving ? "Checking…" : mode === "add" ? "Add test case" : "Save changes"}
          </Button>
          <Button
            onClick={() => templateText != null && setText(templateText)}
            disabled={templateText == null || text === templateText}
          >
            Reset to template
          </Button>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </section>
    </Card>
  );
}

function CheckStatus({ check, onJump }: { check: Check; onJump: (path: string) => void }) {
  if (check.status === "idle" || check.status === "checking") {
    return <p className="m-0 text-muted">{check.status === "checking" ? "Checking the case…" : "The case is checked as you type."}</p>;
  }
  if (check.status === "ok") {
    return (
      <p className="m-0 flex items-center gap-2 text-good">
        <CheckCircle2 aria-hidden className="h-4 w-4 shrink-0" />
        Looks good. The engine can read this case.
      </p>
    );
  }
  if (check.status === "offline") {
    return <p className="m-0 text-warn">{check.message}</p>;
  }
  const n = check.errors.length;
  return (
    <>
      <p className="m-0 flex items-center gap-2 font-medium text-bad">
        <AlertCircle aria-hidden className="h-4 w-4 shrink-0" />
        {n === 1 ? "1 problem to fix" : `${n} problems to fix`}
      </p>
      <ul className="m-0 flex max-h-72 flex-col gap-2 overflow-y-auto p-0" data-testid="case-errors">
        {check.errors.map((e, i) => (
          <li key={`${e.path}-${i}`} className="list-none rounded-lg border border-border bg-surface p-2.5">
            {e.path ? (
              <button
                type="button"
                onClick={() => onJump(e.path)}
                className="cursor-pointer rounded font-mono text-xs break-all text-accent hover:underline"
              >
                {e.path}
                {e.line != null && <span className="text-muted"> (line {e.line})</span>}
              </button>
            ) : (
              e.line != null && <span className="font-mono text-xs text-muted">Line {e.line}</span>
            )}
            <p className="m-0 mt-0.5 text-sm">{e.message}</p>
          </li>
        ))}
      </ul>
    </>
  );
}
