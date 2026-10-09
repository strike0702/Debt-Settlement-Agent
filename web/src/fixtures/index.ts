/**
 * Typed access to the recorded fixture call (`?fixture=1`, tests) and the
 * one-entry catalog fixture mode shows. Synthetic data only.
 */
import type { CallEvent } from "@/lib/callState";
import type { ScenarioMeta } from "@/types/protocol";
import raw from "./call_easy_deal.json";

export interface Frame {
  /** ms since the call started */
  t: number;
  ev: CallEvent;
}

export interface Fixture {
  scenario_id: string;
  view: "operator";
  private_values: {
    max_bp: number;
    program_fee_cents_per_row: number;
    bank_fee_cents: number;
    balance_cents: number[];
  };
  frames: Frame[];
}

export const easyDeal = raw as unknown as Fixture;

/**
 * Catalog for fixture mode, which runs without a backend. Live mode loads
 * `/scenarios` (suggestions come from each rep card) and never uses this.
 */
export const FIXTURE_SCENARIOS: ScenarioMeta[] = [
  {
    id: "easy_deal",
    title: "Easy deal",
    description: "The rep asks for 45%. The agent offers less, then settles at their lowest number.",
    expected: "deal",
    suggested: [
      "Sure. We can take up to eight monthly payments, at least one hundred dollars each, all the same amount.",
      "We are looking for forty-five percent of the balance.",
      "Thirty-two is too low. I could do forty-two percent.",
      "Forty percent is my floor. I can't go lower than that.",
      "Yes, that works for us.",
      "No, that's everything. Thanks.",
    ],
  },
];
