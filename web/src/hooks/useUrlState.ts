/**
 * The selected scenario and view live in the URL (Phase 48), so a link opens
 * the same screen and the browser's back and forward buttons step through
 * what the visitor picked.
 *
 * `?scenario=<id>&view=operator|rep` (the same view names as the call socket;
 * the Creditor rep view is `rep`). Other query parameters (`fixture`, `speed`)
 * are kept. A visitor's change pushes a history entry; the first render and a
 * back/forward step only replace it. A back/forward step that App refuses
 * (the scenario cannot change while a call runs) puts the URL back to what is
 * on screen. This hook does not decide what is selectable; App does.
 */
import { useEffect, useLayoutEffect, useRef } from "react";
import type { Lens } from "@/lib/lens";
import { viewFor } from "@/lib/lens";

export interface UrlState {
  scenario: string | null;
  lens: Lens | null;
}

/** Read `?scenario=` and `?view=` (unknown views are ignored). */
export function readUrlState(search: string = window.location.search): UrlState {
  const q = new URLSearchParams(search);
  const view = q.get("view");
  const scenario = q.get("scenario")?.trim() || null;
  return { scenario, lens: view === "rep" ? "creditor" : view === "operator" ? "operator" : null };
}

/** `href` with `scenario` (when given) and `view` set, other parameters kept. */
export function urlWith(href: string, s: { scenario: string | null; lens: Lens }): string {
  const url = new URL(href);
  if (s.scenario) url.searchParams.set("scenario", s.scenario);
  else url.searchParams.delete("scenario");
  url.searchParams.set("view", viewFor(s.lens));
  return url.toString();
}

export interface UrlStateBinding {
  scenario: string | null;
  lens: Lens;
  /** Apply a scenario from back/forward; return false to refuse it. */
  onScenario: (id: string) => boolean;
  onLens: (lens: Lens) => void;
}

export function useUrlState({ scenario, lens, onScenario, onLens }: UrlStateBinding): void {
  const first = useRef(true);
  const popped = useRef(false);
  const current = useRef({ scenario, lens });
  useLayoutEffect(() => {
    current.current = { scenario, lens };
  });

  useEffect(() => {
    const next = urlWith(window.location.href, { scenario, lens });
    if (next !== window.location.href) {
      if (first.current || popped.current) window.history.replaceState(window.history.state, "", next);
      else window.history.pushState(null, "", next);
    }
    first.current = false;
    popped.current = false;
  }, [scenario, lens]);

  useEffect(() => {
    const onPop = () => {
      const want = readUrlState();
      const now = current.current;
      let changed = false;
      if (want.lens && want.lens !== now.lens) {
        onLens(want.lens);
        changed = true;
      }
      if (want.scenario && want.scenario !== now.scenario) {
        if (onScenario(want.scenario)) changed = true;
        else window.history.replaceState(window.history.state, "", urlWith(window.location.href, { ...now, lens: want.lens ?? now.lens }));
      }
      // The URL already matches; the state change it causes must not push again.
      popped.current = changed;
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [onScenario, onLens]);
}
