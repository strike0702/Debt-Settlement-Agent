/**
 * "Known creditor rules": the creditor's rules the operator already knows, entered
 * before Start call (Debt negotiator view only; App hides it while a call runs and
 * never renders it in the Creditor rep view). Pre-filled with the firm's saved rules
 * for this creditor (`brief.firm.known_rules`); what is entered goes in that call's
 * `start.known_rules`, and the agent plans with it from the first turn without asking.
 *
 * Every field is optional. Styled like `CushionField`: each box's border shows its
 * state with no extra text (green once a value is entered and accepted on Enter or
 * blur, neutral grey while it is being changed or when empty, red when it does not
 * parse or does not fit the other rules). Help and errors sit in a tooltip bubble
 * under the box (`role="tooltip"`, the control's `aria-describedby`); an error keeps
 * it open. The panel's own description is a tooltip on its title (no text under it). No focus ring: the caret shows focus. Payment tiers are a small table
 * (from payment # → minimum $) with add and remove buttons; its error shows under
 * the table. "Fill from scenario" copies the scenario's true rules (demo shortcut).
 */
import { Plus, X } from "lucide-react";
import { type ReactNode, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { type RuleErrors, type RuleKey, type RulesForm, type Structure, type TierRow, tierRow } from "@/lib/knownRules";

export type RuleState = "accepted" | "editing" | "invalid" | "empty";

const BORDER: Record<RuleState, string> = {
  accepted: "border-good",
  editing: "border-border",
  empty: "border-border",
  invalid: "border-bad",
};

/** The panel's description: a tooltip on its title (hover or keyboard focus). */
export const PANEL_HINT =
  "Rules you already know before the call. The agent won't ask for them and reads them back before agreeing. Fill from scenario is a demo shortcut.";

/** Help text per field (tooltip and accessible description). */
export const HELP: Record<RuleKey, string> = {
  max_payments: "The most payments the creditor allows.",
  min_payment_cents: "The smallest single payment the creditor takes.",
  payment_structure: "Even: equal payments. Balloon: a bigger last payment. Flexible: amounts can vary.",
  min_payment_tiers: "Higher minimums later in the plan, for example at least $200 from payment 4 on.",
  max_segments: "How many different payment amounts the plan may use.",
  max_token_pays: "How many payments may be just the minimum amount.",
  first_payment_date: "When the creditor wants the first payment.",
};

const LABEL: Record<RuleKey, string> = {
  max_payments: "Max payments",
  min_payment_cents: "Minimum payment",
  payment_structure: "Payment structure",
  min_payment_tiers: "Payment tiers",
  max_segments: "Max payment levels",
  max_token_pays: "Token payments",
  first_payment_date: "First payment date",
};

const STRUCTURES: { value: Structure; label: string }[] = [
  { value: "", label: "Not known" },
  { value: "even", label: "Even" },
  { value: "balloon", label: "Balloon" },
  { value: "flexible", label: "Flexible" },
];

// Inline: the global unlayered `:focus-visible` outline beats utility classes (see CushionField).
const NO_OUTLINE = { outline: "none" } as const;
const INPUT = "num w-full min-w-0 border-0 bg-transparent text-base shadow-none";

function Box({
  name,
  state,
  error,
  prefix,
  children,
}: {
  name: RuleKey;
  state: RuleState;
  error?: string;
  prefix?: string;
  children: (ids: { id: string; describedBy: string }) => ReactNode;
}) {
  const id = useId();
  const open = error ? "block" : "hidden group-hover:block group-focus-within:block";
  return (
    <div className="group relative flex min-w-0 flex-col gap-1" data-testid={`rule-${name}`}>
      <label htmlFor={id} className="text-sm text-muted">
        {LABEL[name]}
      </label>
      <div className={`flex h-10 items-center rounded-lg border bg-bg px-3 ${BORDER[state]}`} data-state={state}>
        {prefix && (
          <span aria-hidden className="pr-1 text-muted">
            {prefix}
          </span>
        )}
        {children({ id, describedBy: `${id}-hint` })}
      </div>
      <span
        id={`${id}-hint`}
        role="tooltip"
        className={`${open} absolute top-full left-0 z-20 mt-1.5 w-max max-w-[min(18rem,calc(100vw-2rem))] rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs shadow-lg ${error ? "text-bad" : "text-fg"}`}
      >
        {error ?? HELP[name]}
      </span>
    </div>
  );
}

export function KnownRulesPanel({
  form,
  errors,
  asOf,
  onForm,
  onFill,
}: {
  form: RulesForm;
  errors: RuleErrors;
  /** The call's as-of date (ISO): the earliest first payment date. */
  asOf?: string;
  onForm: (form: RulesForm) => void;
  /** Fill from the scenario's true rules; absent for a custom case (no true rules). */
  onFill?: () => void;
}) {
  // Fields changed since their last Enter or blur: grey until accepted.
  const [dirty, setDirty] = useState<ReadonlySet<RuleKey>>(new Set());
  const mark = (k: RuleKey, on: boolean) =>
    setDirty((d) => {
      const next = new Set(d);
      if (on) next.add(k);
      else next.delete(k);
      return next;
    });
  const set = <K extends RuleKey>(k: K, v: RulesForm[K]) => {
    mark(k, true);
    onForm({ ...form, [k]: v });
  };
  const hasValue = (k: RuleKey) => {
    const v = form[k];
    return Array.isArray(v) ? v.some((r) => r.from.trim() || r.min.trim()) : v.trim() !== "";
  };
  const state = (k: RuleKey): RuleState =>
    errors[k] ? "invalid" : dirty.has(k) ? "editing" : hasValue(k) ? "accepted" : "empty";
  const accept = (k: RuleKey) => ({
    onBlur: () => mark(k, false),
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        e.preventDefault();
        mark(k, false);
      }
    },
  });
  const text = (k: Exclude<RuleKey, "min_payment_tiers" | "payment_structure">, prefix?: string, type = "text") => (
    <Box name={k} state={state(k)} error={errors[k]} prefix={prefix}>
      {({ id, describedBy }) => (
        <input
          id={id}
          name={k}
          type={type}
          min={type === "date" ? asOf : undefined}
          inputMode={type === "date" ? undefined : prefix ? "decimal" : "numeric"}
          autoComplete="off"
          spellCheck={false}
          placeholder={type === "date" ? undefined : "Not known"}
          value={form[k]}
          onChange={(e) => set(k, e.target.value)}
          {...accept(k)}
          aria-invalid={errors[k] ? true : undefined}
          aria-describedby={describedBy}
          className={INPUT}
          style={NO_OUTLINE}
        />
      )}
    </Box>
  );
  const tiers = form.min_payment_tiers;
  const setTier = (key: number, patch: Partial<TierRow>) =>
    set("min_payment_tiers", tiers.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const tierState = state("min_payment_tiers");
  const tierHint = useId();
  const titleHint = useId();

  return (
    <Card data-testid="known-rules">
      <CardHeader
        title={
          <span className="group relative">
            <span tabIndex={0} aria-describedby={titleHint} className="cursor-help">
              Known creditor rules
            </span>
            <span
              id={titleHint}
              role="tooltip"
              className="absolute top-full left-0 z-20 mt-1.5 hidden w-max max-w-[min(18rem,calc(100vw-2rem))] rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs font-normal text-fg shadow-lg group-focus-within:block group-hover:block"
            >
              {PANEL_HINT}
            </span>
          </span>
        }
        aside={
          onFill && (
            <Button size="sm" variant="ghost" className="text-accent" onClick={onFill} title="Demo shortcut: copies this scenario's real rules">
              Fill from scenario
            </Button>
          )
        }
      />
      <CardBody className="flex flex-col gap-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {text("max_payments")}
          {text("min_payment_cents", "$")}
          <Box name="payment_structure" state={state("payment_structure")}>
            {({ id, describedBy }) => (
              <select
                id={id}
                name="payment_structure"
                value={form.payment_structure}
                onChange={(e) => {
                  onForm({ ...form, payment_structure: e.target.value as Structure });
                }}
                aria-describedby={describedBy}
                className="w-full min-w-0 border-0 bg-transparent text-base text-fg"
                style={NO_OUTLINE}
              >
                {STRUCTURES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            )}
          </Box>
          {text("max_segments")}
          {text("max_token_pays")}
          {text("first_payment_date", undefined, "date")}
        </div>

        <fieldset className="m-0 flex flex-col gap-1.5 border-0 p-0" data-testid="rule-min_payment_tiers">
          <legend className="group relative mb-1 p-0 text-sm text-muted">
            <span aria-describedby={tierHint} tabIndex={0} className="cursor-help">
              {LABEL.min_payment_tiers}
            </span>
            <span
              id={tierHint}
              role="tooltip"
              className="absolute top-full left-0 z-20 mt-1.5 hidden w-max max-w-[min(18rem,calc(100vw-2rem))] rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs text-fg shadow-lg group-focus-within:block group-hover:block"
            >
              {HELP.min_payment_tiers}
            </span>
          </legend>
          {tiers.length > 0 && (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-muted">
                  <th className="pb-1 font-normal">From payment #</th>
                  <th className="pb-1 pl-2 font-normal">Minimum</th>
                  <th className="w-9" aria-label="Remove" />
                </tr>
              </thead>
              <tbody>
                {tiers.map((r, i) => (
                  <tr key={r.key}>
                    <td className="py-1 pr-2">
                      <div className={`flex h-9 items-center rounded-lg border bg-bg px-3 ${BORDER[tierState]}`} data-state={tierState}>
                        <input
                          name={`tier-${i + 1}-from`}
                          aria-label={`Tier ${i + 1} from payment number`}
                          inputMode="numeric"
                          autoComplete="off"
                          value={r.from}
                          onChange={(e) => setTier(r.key, { from: e.target.value })}
                          {...accept("min_payment_tiers")}
                          aria-invalid={errors.min_payment_tiers ? true : undefined}
                          className={INPUT}
                          style={NO_OUTLINE}
                        />
                      </div>
                    </td>
                    <td className="py-1 pl-2">
                      <div className={`flex h-9 items-center rounded-lg border bg-bg px-3 ${BORDER[tierState]}`} data-state={tierState}>
                        <span aria-hidden className="pr-1 text-muted">
                          $
                        </span>
                        <input
                          name={`tier-${i + 1}-min`}
                          aria-label={`Tier ${i + 1} minimum payment`}
                          inputMode="decimal"
                          autoComplete="off"
                          value={r.min}
                          onChange={(e) => setTier(r.key, { min: e.target.value })}
                          {...accept("min_payment_tiers")}
                          aria-invalid={errors.min_payment_tiers ? true : undefined}
                          className={INPUT}
                          style={NO_OUTLINE}
                        />
                      </div>
                    </td>
                    <td className="py-1 pl-1 text-right">
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={`Remove tier ${i + 1}`}
                        onClick={() => set("min_payment_tiers", tiers.filter((t) => t.key !== r.key))}
                      >
                        <X className="h-4 w-4" aria-hidden />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {errors.min_payment_tiers && (
            <p className="m-0 text-xs text-bad" role="alert">
              {errors.min_payment_tiers}
            </p>
          )}
          <div>
            <Button size="sm" variant="ghost" className="text-accent" onClick={() => set("min_payment_tiers", [...tiers, tierRow()])}>
              <Plus className="h-4 w-4" aria-hidden /> Add a tier
            </Button>
          </div>
        </fieldset>
      </CardBody>
    </Card>
  );
}
