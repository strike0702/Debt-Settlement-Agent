/**
 * Left column: the call as the two speakers hear it.
 *
 * Chat bubbles (agent left, creditor rep right), the mic with its
 * listening/thinking/speaking state, suggested rep replies from the scenario
 * card, and a text box. 23a renders state only; `onSend` / `onMicToggle`
 * are wired to the socket and VAD in 23b and are optional here.
 */
import { Mic, MicOff, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import type { ChatMessage } from "@/lib/callState";
import { cn } from "@/lib/cn";
import { MIC_LABEL, type MicState } from "@/lib/mic";

export interface ConversationProps {
  messages: ChatMessage[];
  mic: MicState;
  suggested: string[];
  /** Index of the next suggestion the scripted call would say (highlighted). */
  nextSuggested?: number;
  onSend?: (text: string) => void;
  onMicToggle?: () => void;
  emptyHint: string;
}

const MIC_DOT: Record<MicState, string> = {
  off: "bg-faint",
  listening: "bg-good animate-pulse",
  thinking: "bg-warn animate-pulse",
  speaking: "bg-accent animate-pulse",
};

export function Conversation({
  messages,
  mic,
  suggested,
  nextSuggested,
  onSend,
  onMicToggle,
  emptyHint,
}: ConversationProps) {
  const [draft, setDraft] = useState("");
  const log = useRef<HTMLDivElement>(null);
  // Scroll the transcript box only; scrollIntoView would also scroll the page.
  useEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const send = (text: string) => {
    if (!onSend || !text.trim()) return;
    onSend(text.trim());
    setDraft("");
  };

  return (
    <Card className="flex min-h-0 flex-col">
      <CardHeader
        title="Conversation"
        aside={
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-2 text-sm text-muted" aria-live="polite">
              <span className={cn("h-2 w-2 rounded-full", MIC_DOT[mic])} aria-hidden />
              {MIC_LABEL[mic]}
            </span>
            <Button
              size="icon"
              variant={mic === "off" ? "outline" : "primary"}
              aria-label={mic === "off" ? "Turn mic on" : "Turn mic off"}
              aria-pressed={mic !== "off"}
              onClick={onMicToggle}
              disabled={!onMicToggle}
            >
              {mic === "off" ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
            </Button>
          </div>
        }
      />

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
      </div>

      {suggested.length > 0 && (
        <div className="border-t border-border px-4 pt-3">
          <p className="mb-2 text-sm text-muted">Suggested rep replies</p>
          <ul className="flex flex-col gap-1.5">
            {suggested.map((s, i) => (
              <li key={s}>
                <button
                  type="button"
                  disabled={!onSend}
                  onClick={() => send(s)}
                  className={cn(
                    "w-full rounded-lg border px-3 py-1.5 text-left text-sm",
                    i === nextSuggested ? "border-accent text-fg" : "border-border text-muted",
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
          placeholder={onSend ? "Type as the creditor rep…" : "Replaying a recorded call"}
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
