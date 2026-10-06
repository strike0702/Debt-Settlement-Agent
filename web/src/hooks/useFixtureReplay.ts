/**
 * Replays recorded frames with their original timing (scaled by `speed`).
 *
 * Fixture mode only (`?fixture=1`). Returns the raw operator events seen so
 * far; the caller applies the lens filter and folds them, so switching lens
 * mid-replay re-derives the view without restarting.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Frame } from "@/fixtures";
import type { ServerEvent } from "@/types/events";

export interface Replay {
  events: ServerEvent[];
  playing: boolean;
  done: boolean;
  start: () => void;
  stop: () => void;
}

export function useFixtureReplay(frames: readonly Frame[], speed = 1): Replay {
  const [count, setCount] = useState(0);
  const [running, setRunning] = useState(false);
  const timer = useRef<number | null>(null);
  const done = count >= frames.length && frames.length > 0;
  const playing = running && !done;

  const stop = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    setRunning(false);
  }, []);

  const start = useCallback(() => {
    stop();
    setCount(0);
    setRunning(true);
  }, [stop]);

  useEffect(() => {
    if (!playing) return;
    const prevT = count === 0 ? 0 : (frames[count - 1]?.t ?? 0);
    // Release every frame that shares this timestamp in one render.
    let next = count + 1;
    const t = frames[count]?.t ?? 0;
    while (next < frames.length && frames[next]?.t === t) next += 1;
    const delay = Math.max(0, (t - prevT) / Math.max(speed, 0.01));
    timer.current = window.setTimeout(() => setCount(next), delay);
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [playing, count, frames, speed]);

  const events = frames.slice(0, count).map((f) => f.ev);
  return { events, playing, done, start, stop };
}
