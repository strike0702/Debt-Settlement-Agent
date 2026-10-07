/**
 * Scenario catalog (`GET /scenarios`), the operator brief, and the rep's own
 * account card, for a curated case or a visitor's custom case.
 *
 * Curated: `GET /scenarios/{id}` (brief) and `GET /scenarios/{id}/rep`.
 * Custom (Phase 36): `POST /scenarios/preview` and `POST /scenarios/preview/rep`
 * with the case's JSON; the rep endpoint reads only the creditor side.
 *
 * The brief holds the client's private finances and the firm's fees, so it is
 * fetched only while the operator lens is on; switching to the Creditor rep view
 * drops it from memory. The rep account holds only creditor-side data and is
 * what the Creditor rep view shows instead. Fixture mode passes `fallback` and
 * never fetches.
 */
import { useEffect, useState } from "react";
import { type ScenarioSource, sourceKey } from "@/lib/customCases";
import type { Lens } from "@/lib/lens";
import type { RepAccount, ScenarioBrief, ScenarioMeta } from "@/types/protocol";

function postJson(url: string, body: unknown): Promise<Response> {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

function briefRequest(src: ScenarioSource): Promise<Response> {
  return src.kind === "catalog" ? fetch(`/scenarios/${encodeURIComponent(src.id)}`) : postJson("/scenarios/preview", src.payload);
}

function repRequest(src: ScenarioSource): Promise<Response> {
  return src.kind === "catalog" ? fetch(`/scenarios/${encodeURIComponent(src.id)}/rep`) : postJson("/scenarios/preview/rep", src.payload);
}

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

export function useScenarioBrief(src: ScenarioSource | null, lens: Lens, enabled: boolean): ScenarioBrief | null {
  // Keyed by source so a stale brief is never shown for another scenario, edit or lens.
  const [loaded, setLoaded] = useState<{ key: string; brief: ScenarioBrief | null } | null>(null);
  const want = enabled && lens === "operator" && src ? src : null;
  const key = want ? sourceKey(want) : null;
  useEffect(() => {
    if (!want || !key) return;
    let live = true;
    briefRequest(want)
      .then((r) => (r.ok ? (r.json() as Promise<ScenarioBrief>) : null))
      .then((brief) => live && setLoaded({ key, brief }))
      .catch(() => live && setLoaded({ key, brief: null }));
    return () => {
      live = false;
    };
    // `key` covers `want` (a new source or a new version of a custom case).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return key && loaded?.key === key ? loaded.brief : null;
}

export type RepAccountState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; account: RepAccount };

/** The creditor's own account and rules for a scenario (keyed like the brief, so never stale). */
export function useRepAccount(src: ScenarioSource): RepAccountState {
  const [loaded, setLoaded] = useState<{ key: string; state: RepAccountState } | null>(null);
  const key = sourceKey(src);
  useEffect(() => {
    let live = true;
    repRequest(src)
      .then((r) => (r.ok ? (r.json() as Promise<RepAccount>) : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((account) => live && setLoaded({ key, state: { status: "ready", account } }))
      .catch((e: unknown) => live && setLoaded({ key, state: { status: "error", message: String(e) } }));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return loaded?.key === key ? loaded.state : { status: "loading" };
}

/**
 * `GET /scenarios/template`, pretty-printed for the test-case editor. Fetched
 * the first time `enabled` is true, then kept. On failure it falls back to an
 * empty object so the editor still opens (and its checker says what is missing).
 */
export function useScenarioTemplate(enabled: boolean): string | null {
  const [text, setText] = useState<string | null>(null);
  const want = enabled && text === null;
  useEffect(() => {
    if (!want) return;
    let live = true;
    fetch("/scenarios/template")
      .then((r) => (r.ok ? (r.json() as Promise<unknown>) : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((tpl) => live && setText(JSON.stringify(tpl, null, 2)))
      .catch(() => live && setText("{\n  \n}"));
    return () => {
      live = false;
    };
  }, [want]);
  return text;
}
