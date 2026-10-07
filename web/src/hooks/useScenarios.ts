/**
 * Scenario catalog (`GET /scenarios`), the operator brief (`GET /scenarios/{id}`),
 * and the rep's own account card (`GET /scenarios/{id}/rep`).
 *
 * The brief holds the client's private finances and the firm's fees, so it is
 * fetched only while the operator lens is on; switching to the creditor's eye
 * drops it from memory. The rep account holds only creditor-side data and is
 * what the creditor's eye shows instead. Fixture mode passes `fallback` and
 * never fetches.
 */
import { useEffect, useState } from "react";
import type { Lens } from "@/lib/lens";
import type { RepAccount, ScenarioBrief, ScenarioMeta } from "@/types/protocol";

export function useScenarios(fallback: ScenarioMeta[] | null): { scenarios: ScenarioMeta[]; error: string | null } {
  const [scenarios, setScenarios] = useState<ScenarioMeta[]>(fallback ?? []);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (fallback) return;
    let live = true;
    fetch("/scenarios")
      .then((r) => (r.ok ? (r.json() as Promise<ScenarioMeta[]>) : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((rows) => live && setScenarios(rows.map((r) => ({ ...r, suggested: r.suggested ?? [] }))))
      .catch((e: unknown) => live && setError(`Could not load scenarios (${String(e)}).`));
    return () => {
      live = false;
    };
  }, [fallback]);
  return { scenarios, error };
}

export function useScenarioBrief(id: string | null, lens: Lens, enabled: boolean): ScenarioBrief | null {
  // Keyed by id so a stale brief is never shown for another scenario or lens.
  const [loaded, setLoaded] = useState<{ id: string; brief: ScenarioBrief | null } | null>(null);
  const want = enabled && lens === "operator" && id ? id : null;
  useEffect(() => {
    if (!want) return;
    let live = true;
    fetch(`/scenarios/${encodeURIComponent(want)}`)
      .then((r) => (r.ok ? (r.json() as Promise<ScenarioBrief>) : null))
      .then((brief) => live && setLoaded({ id: want, brief }))
      .catch(() => live && setLoaded({ id: want, brief: null }));
    return () => {
      live = false;
    };
  }, [want]);
  return want && loaded?.id === want ? loaded.brief : null;
}

export type RepAccountState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; account: RepAccount };

/** The creditor's own account and rules for scenario `id` (keyed like the brief, so never stale). */
export function useRepAccount(id: string): RepAccountState {
  const [loaded, setLoaded] = useState<{ id: string; state: RepAccountState } | null>(null);
  useEffect(() => {
    let live = true;
    fetch(`/scenarios/${encodeURIComponent(id)}/rep`)
      .then((r) => (r.ok ? (r.json() as Promise<RepAccount>) : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((account) => live && setLoaded({ id, state: { status: "ready", account } }))
      .catch((e: unknown) => live && setLoaded({ id, state: { status: "error", message: String(e) } }));
    return () => {
      live = false;
    };
  }, [id]);
  return loaded?.id === id ? loaded.state : { status: "loading" };
}
