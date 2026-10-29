/**
 * Voice controls for the Pipecat stack (WebRTC), shown in the conversation panel.
 *
 * `VoiceStatus` sits in the panel header: the call state at a glance
 * (connecting, listening, rep speaking, thinking, agent speaking, muted,
 * reconnecting) and the voice on/off button. `VoiceBar` sits above the text
 * box while voice is on: mute, push to talk, and the hold-to-talk button.
 * Display only: `useWebRtcVoice` owns the connection. The legacy stack keeps
 * the mic button inside `Conversation`.
 */
import { Hand, Mic, MicOff, Phone, PhoneOff } from "lucide-react";
import type { KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { VOICE_LABEL, type CallVoiceState } from "@/lib/voice/webrtc";

const DOT: Record<CallVoiceState, string> = {
  off: "bg-faint",
  connecting: "bg-warn animate-pulse",
  reconnecting: "bg-warn animate-pulse",
  listening: "bg-good",
  rep_speaking: "bg-good animate-pulse",
  thinking: "bg-warn animate-pulse",
  agent_speaking: "bg-accent animate-pulse",
  muted: "bg-bad",
};

export function VoiceStatus({ state, onToggle }: { state: CallVoiceState; onToggle?: () => void }) {
  const on = state !== "off";
  return (
    <div className="flex items-center gap-2">
      <span className="inline-flex items-center gap-2 text-sm whitespace-nowrap text-muted" aria-live="polite" data-testid="voice-state">
        <span className={cn("h-2 w-2 rounded-full motion-reduce:animate-none", DOT[state])} aria-hidden />
        {VOICE_LABEL[state]}
      </span>
      <Button
        size="sm"
        variant={on ? "primary" : "outline"}
        aria-pressed={on}
        onClick={onToggle}
        disabled={!onToggle}
      >
        {on ? <PhoneOff className="h-4 w-4" aria-hidden /> : <Phone className="h-4 w-4" aria-hidden />}
        {on ? "Turn voice off" : "Turn voice on"}
      </Button>
    </div>
  );
}

export interface VoiceBarProps {
  muted: boolean;
  pushToTalk: boolean;
  talking: boolean;
  onMute: (muted: boolean) => void;
  onPushToTalk: (on: boolean) => void;
  onTalk: (down: boolean) => void;
}

export function VoiceBar({ muted, pushToTalk, talking, onMute, onPushToTalk, onTalk }: VoiceBarProps) {
  const key = (down: boolean) => (e: KeyboardEvent) => {
    if (e.key !== " " && e.key !== "Enter") return;
    e.preventDefault();
    if (!e.repeat) onTalk(down);
  };
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-border px-4 pt-3" data-testid="voice-bar">
      <Button size="sm" aria-pressed={muted} onClick={() => onMute(!muted)}>
        {muted ? <MicOff className="h-4 w-4" aria-hidden /> : <Mic className="h-4 w-4" aria-hidden />}
        {muted ? "Unmute" : "Mute"}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        className={cn(pushToTalk && "bg-surface-2")}
        aria-pressed={pushToTalk}
        onClick={() => onPushToTalk(!pushToTalk)}
      >
        <Hand className="h-4 w-4" aria-hidden />
        {pushToTalk ? "Push to talk on" : "Push to talk"}
      </Button>
      {pushToTalk && (
        <Button
          size="sm"
          variant={talking ? "primary" : "outline"}
          className="flex-1 touch-none select-none"
          aria-pressed={talking}
          disabled={muted}
          onPointerDown={(e) => {
            e.currentTarget.setPointerCapture?.(e.pointerId);
            onTalk(true);
          }}
          onPointerUp={() => onTalk(false)}
          onPointerCancel={() => onTalk(false)}
          onKeyDown={key(true)}
          onKeyUp={key(false)}
          onBlur={() => talking && onTalk(false)}
        >
          {talking ? "Talking… release to stop" : "Hold to talk"}
        </Button>
      )}
    </div>
  );
}
