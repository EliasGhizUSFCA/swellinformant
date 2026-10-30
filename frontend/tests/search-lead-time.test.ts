import { describe, expect, it } from "vitest";

import { shortNoticeLabel } from "@/components/search/use-search-defaults";
import type { Search } from "@/types/api";
import { DEFAULT_ALERT_LEAD_DAYS, defaultWizardState, fromSearch, MAX_ALERT_LEAD_DAYS, MAX_ORIGINS, toSearchInput, validateStep } from "@/lib/validation";

describe("search wizard: alert lead time (spec 28)", () => {
  it("starts from the deployment's defaults, else 2-10 days", () => {
    expect(DEFAULT_ALERT_LEAD_DAYS).toEqual({ min: 2, max: 10 });
    const built = defaultWizardState("SYD");
    expect([built.alert_lead_min_days, built.alert_lead_max_days]).toEqual([2, 10]);
    const configured = defaultWizardState("SYD", "m", { min: 3, max: 8 });
    expect([configured.alert_lead_min_days, configured.alert_lead_max_days]).toEqual([3, 8]);
  });

  it("validates whole days from 0 to 16 with the earliest not after the latest", () => {
    const ok = { ...defaultWizardState("SYD"), alert_lead_min_days: 1, alert_lead_max_days: 7 };
    expect(validateStep("dates", ok)).toEqual({});
    expect(validateStep("dates", { ...ok, alert_lead_min_days: 8 })).toHaveProperty("alert_lead_max_days");
    expect(validateStep("dates", { ...ok, alert_lead_min_days: -1 })).toHaveProperty("alert_lead_min_days");
    expect(validateStep("dates", { ...ok, alert_lead_max_days: MAX_ALERT_LEAD_DAYS + 1 })).toHaveProperty("alert_lead_max_days");
    expect(validateStep("dates", { ...ok, alert_lead_min_days: 1.5 })).toHaveProperty("alert_lead_min_days");
    // The review step checks it too, so a bad value can never be submitted.
    expect(validateStep("review", { ...ok, name: "Bali", alert_lead_min_days: 9, alert_lead_max_days: 2 })).toHaveProperty(
      "alert_lead_max_days",
    );
  });

  it("sends the lead days and up to five origins to the API and reads them back", () => {
    const origins = ["SYD", "MEL", "BNE", "PER", "OOL"];
    const state = { ...defaultWizardState("SYD"), name: "Bali", origins, alert_lead_min_days: 1, alert_lead_max_days: 6, max_layovers: 3 };
    expect(validateStep("flights", state)).toEqual({});
    expect(validateStep("flights", { ...state, origins: [...origins, "SFO"] })).toHaveProperty("origins");
    expect(MAX_ORIGINS).toBe(5);
    const body = toSearchInput(state);
    expect([body.alert_lead_min_days, body.alert_lead_max_days]).toEqual([1, 6]);
    expect(body.origins).toEqual(origins);
    expect(body.travel.max_layovers).toBe(3);
    // What the API returns for that search (SearchOut) reads back into the same state.
    const saved = {
      ...body,
      units: "ft",
      wave_preferred: null,
      destinations: { spot_slugs: [], region_groups: [], countries: [] },
      travel: { ...body.travel, max_price: "1400.00" },
    } as unknown as Search;
    const back = fromSearch(saved);
    expect([back.alert_lead_min_days, back.alert_lead_max_days]).toEqual([1, 6]);
    expect(back.origins).toEqual(origins);
  });

  it("labels the short-notice band from the server, else 3-7 days", () => {
    expect(shortNoticeLabel(undefined)).toBe("3–7 days ahead");
    expect(
      shortNoticeLabel({
        alert_lead_min_days: 2,
        alert_lead_max_days: 10,
        forecast_days: 10,
        detection_lead_min_days: 1,
        detection_lead_max_days: 10,
        short_notice_min_days: 2,
        short_notice_max_days: 5,
        max_origins: 5,
        max_layovers: 3,
        max_alert_lead_days: 16,
        flight_searches_per_day: 20,
      }),
    ).toBe("2–5 days ahead");
  });
});
