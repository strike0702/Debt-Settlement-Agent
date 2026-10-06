/**
 * Typed access to the recorded fixture call and the static scenario catalog
 * used until 23b fetches `/scenarios`. Synthetic data only.
 */
import type { ScenarioMeta, ServerEvent } from "@/types/events";
import raw from "./call_easy_deal.json";

export interface Frame {
  /** ms since the call started */
  t: number;
  ev: ServerEvent;
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

export interface ScenarioCard extends ScenarioMeta {
  /** Lines from the scenario's rep card a visitor can click, in call order. */
  suggested: string[];
}

export const SCENARIOS: ScenarioCard[] = [
  {
    id: "easy_deal",
    title: "Easy deal",
    description: "Even payments, 45% ask. Reaches a deal with the scripted replies.",
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
  {
    id: "counter_ladder",
    title: "Counter ladder",
    description: "Rep opens at 95%; the agent counters down toward a feasible percentage.",
    expected: "counter",
    suggested: [],
  },
  {
    id: "late_start_date",
    title: "Late start date",
    description: "Rep wants a first payment after the savings horizon; the agent proposes an earlier date.",
    expected: "deal",
    suggested: [],
  },
  {
    id: "balloon_structure",
    title: "Balloon structure",
    description: "Rep requires a balloon-friendly payment structure.",
    expected: "deal",
    suggested: [],
  },
  {
    id: "no_space",
    title: "No deal",
    description: "The rules leave no feasible schedule for a realistic ask.",
    expected: "no_deal",
    suggested: [],
  },
  {
    id: "rescue_escalate",
    title: "Rescue / escalate",
    description: "Tight client capacity; the schedule needs extra funds, so the agent escalates.",
    expected: "escalate",
    suggested: [],
  },
];
