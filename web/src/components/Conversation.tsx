/**
 * Left column: the call as the two speakers hear it.
 *
 * Chat bubbles (agent left, creditor rep right), suggested rep replies from
 * the scenario card, and a text box. `onSend` is wired by App to the call
 * socket; when absent (replay, no call) the controls are disabled.
 * `voiceStatus` (header) and `voiceBar` (above the replies) come from
 * `VoiceControls`; `interim` is Flux's live partial.
 * `ended` closes the transcript with a quiet line saying how the
 * call ended ("Call ended: handed off to a specialist").
 * `error` is the newest call error, shown as an alert above the text box
 * (plain copy; `detail` is the raw server text, Debt negotiator view only).
 * A new error puts the rep's last sent line back in an empty text box, so
 * "try sending it again" is one click.
 */
import { AlertTriangle, Send, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import type { ChatMessage } from "@/lib/callState";
import { cn } from "@/lib/cn";

export interface ConversationProps {
  messages: ChatMessage[];
  /**
   * Rep lines to click, shown once each. All look the same: no "next"
   * highlight, since a rep who haggles their own way makes the scripted order meaningless.
   */
  suggested: string[];
  onSend?: (text: string) => void;
  emptyHint: string;
  /** Shown under the last bubble once the call is over (how it ended). */
  ended?: string | null;
  /** Placeholder for the text box when sending is disabled. */
  idleHint?: string;
  interim?: string;
  notice?: string;
  onDismissNotice?: () => void;
  /** Newest call error; `seq` changes with each new one. */
  error?: { text: string; detail?: string | null; detailLabel?: string; seq: number } | null;
  /** Voice state and the voice on/off button, in the header. */
  voiceStatus?: ReactNode;
  /** WebRTC voice controls (mute, push to talk), shown while voice is on. */
  voiceBar?: ReactNode;
}

export function Conversation({
  messages,
  suggested,
  onSend,
  emptyHint,
  ended,
  idleHint = "Replaying a recorded call",
  interim,
  notice,
  onDismissNotice,
  error,
  voiceStatus,
  voiceBar,
}: ConversationProps) {
  const [draft, setDraft] = useState("");
  const lastSent = useRef("");
  const errorSeq = error?.seq;
  // A failed line comes back to the empty text box; a draft already typed is kept.
  useEffect(() => {
    if (errorSeq != null && lastSent.current) setDraft((d) => d || lastSent.current);
  }, [errorSeq]);
  const log = useRef<HTMLDivElement>(null);
  // Scroll the transcript box only; scrollIntoView would also scroll the page.
  useEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, interim, ended]);

  // Scripts repeat lines (a rep restates their number); the list shows each once, in first order.
  const unique = [...new Set(suggested)];
  const send = (text: string) => {
    if (!onSend || !text.trim()) return;
    lastSent.current = text.trim();
    onSend(text.trim());
    setDraft("");
  };

  return (
    <Card className="flex min-h-0 flex-col">
      <CardHeader title="Conversation" aside={voiceStatus} />

      <div
        ref={log}
        className="flex max-h-[36rem] min-h-48 flex-1 flex-col gap-3 overflow-y-auto px-4 py-2"
        aria-label="Transcript"
        role="log"
      >
        {messages.length === 0 ? (
          <p className="m-auto max-w-xs text-center text-muted">{emptyHint}</p>
        ) : (
          messages.map((m) => <Bubble key={m.key} msg={m} />)
        )}
        {interim && (
          <p className="self-end text-sm italic text-muted" aria-live="polite" data-testid="interim">
            {interim}
          </p>
        )}
        {ended && messages.length > 0 && (
          <p className="my-1 flex items-center gap-3 text-sm text-muted" data-testid="call-ended">
            <span className="h-px flex-1 bg-border" aria-hidden />
            <span className="text-center text-balance">{ended}</span>
            <span className="h-px flex-1 bg-border" aria-hidden />
          </p>
        )}
      </div>

      {notice && (
        <div role="status" className="mx-4 mb-2 flex items-start gap-2 rounded-lg border border-warn bg-surface-2 px-3 py-2 text-sm">
          <span className="flex-1">{notice}</span>
          {onDismissNotice && (
            <button type="button" aria-label="Dismiss notice" className="cursor-pointer text-muted hover:text-fg" onClick={onDismissNotice}>
              <X className="h-4 w-4" aria-hidden />
            </button>
          )}
        </div>
      )}

      {error && (
        <div role="alert" data-testid="call-error" className="mx-4 mb-2 flex flex-col gap-0.5 rounded-lg border border-bad bg-surface-2 px-3 py-2 text-sm">
          <span className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-bad" aria-hidden />
            <span>{error.text}</span>
          </span>
          {error.detail && <span className="pl-6 text-xs break-words text-muted">{error.detailLabel ?? "Server message"}: {error.detail}</span>}
        </div>
      )}

      {voiceBar}

      {unique.length > 0 && (
        <div className="border-t border-border px-4 pt-3">
          <p className="mb-2 text-sm text-muted">Suggested rep replies</p>
          <ul className="flex flex-col gap-1.5">
            {unique.map((s) => (
              <li key={s}>
                <button
                  type="button"
                  disabled={!onSend}
                  onClick={() => send(s)}
                  className={cn(
                    "w-full rounded-lg border border-border px-3 py-1.5 text-left text-sm text-muted",
                    onSend ? "cursor-pointer hover:bg-surface-2" : "cursor-default",
                  )}
                >
                  {s}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <form
        className="flex gap-2 p-4"
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
      >
        <label className="sr-only" htmlFor="rep-input">
          Say something as the creditor rep
        </label>
        <input
          id="rep-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={!onSend}
          placeholder={onSend ? "Type as the creditor rep…" : idleHint}
          className="h-10 min-w-0 flex-1 rounded-lg border border-border bg-bg px-3 text-base placeholder:text-faint disabled:opacity-60"
        />
        <Button type="submit" variant="primary" size="icon" aria-label="Send" disabled={!onSend || !draft.trim()}>
          <Send className="h-4 w-4" />
        </Button>
      </form>
    </Card>
  );
}

function Bubble({ msg }: { msg: ChatMessage }) {
  const agent = msg.role === "agent";
  return (
    <div className={cn("flex flex-col gap-1", agent ? "items-start" : "items-end")}>
      <span className="text-xs text-muted">{agent ? "Agent" : "Creditor rep"}</span>
      <div
        className={cn(
          "max-w-[85%] rounded-2xl px-4 py-2.5",
          agent ? "rounded-tl-sm bg-surface-2" : "rounded-tr-sm bg-accent text-accent-fg",
        )}
      >
        {msg.sentences.map((s) => s.text).join(" ")}
      </div>
    </div>
  );
}
