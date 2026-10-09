import { describe, expect, it } from "vitest";

import { ApiError, readCookie } from "@/lib/api";
import { colorForScore, labelForScore, QUALITY_COLORS } from "@/lib/quality";
import { defaultWizardState, registerSchema, safeNext, toSearchInput, validateStep } from "@/lib/validation";

describe("quality scale (mirrors backend thresholds)", () => {
  it.each([
    [100, "exceptional"],
    [95, "exceptional"],
    [94, "excellent"],
    [85, "excellent"],
    [72, "very_good"],
    [57, "good"],
    [40, "fair"],
    [39, "poor"],
  ])("score %i → %s", (score, label) => {
    expect(labelForScore(score)).toBe(label);
  });

  it("colours by label", () => {
    expect(colorForScore(96)).toBe(QUALITY_COLORS.exceptional);
    expect(colorForScore(null)).toBe("#cbd5e1");
  });
});

describe("auth validation", () => {
  it("accepts a valid registration", () => {
    const r = registerSchema.safeParse({ full_name: "Kai", email: "kai@example.com", password: "Barrels4Days!", confirm_password: "Barrels4Days!" });
    expect(r.success).toBe(true);
  });

  it("rejects weak or mismatched passwords and markup in names", () => {
    expect(registerSchema.safeParse({ full_name: "Kai", email: "kai@example.com", password: "short", confirm_password: "short" }).success).toBe(false);
    expect(registerSchema.safeParse({ full_name: "Kai", email: "kai@example.com", password: "Barrels4Days!", confirm_password: "nope" }).success).toBe(false);
    expect(registerSchema.safeParse({ full_name: "<b>", email: "kai@example.com", password: "Barrels4Days!", confirm_password: "Barrels4Days!" }).success).toBe(false);
  });

  it("only redirects to same-site paths after login", () => {
    expect(safeNext("/searches")).toBe("/searches");
    expect(safeNext("https://evil.example")).toBe("/dashboard");
    expect(safeNext("//evil.example")).toBe("/dashboard");
    expect(safeNext(null)).toBe("/dashboard");
  });
});

describe("search wizard", () => {
  it("defaults to the spec's 2-day / 1-day buffers and the home airport", () => {
    const s = defaultWizardState("SFO");
    expect(s.arrival_buffer_days).toBe(2);
    expect(s.departure_buffer_days).toBe(1);
    expect(s.origins).toEqual(["SFO"]);
  });

  it("validates each step", () => {
    const s = { ...defaultWizardState(), wave_min: 10, wave_max: 5 };
    expect(validateStep("surf", s)).toHaveProperty("wave_max");
    expect(validateStep("destinations", { ...s, destination_mode: "spots" })).toHaveProperty("spot_slugs");
    expect(validateStep("flights", defaultWizardState())).toHaveProperty("origins");
    expect(validateStep("flights", { ...defaultWizardState("SFO"), preferred_airlines: "UA, BADCODE" })).toHaveProperty("preferred_airlines");
    const past = { ...defaultWizardState("SFO"), date_mode: "fixed" as const, date_start: "2020-01-01", date_end: "2020-01-05" };
    expect(validateStep("dates", past)).toHaveProperty("date_start");
    expect(validateStep("review", { ...defaultWizardState("SFO"), name: "" })).toHaveProperty("name");
    expect(validateStep("review", { ...defaultWizardState("SFO"), name: "Ok" })).toEqual({});
  });

  it("serialises to the API contract", () => {
    const body = toSearchInput({
      ...defaultWizardState("SFO"),
      name: "  J-Bay  ",
      destination_mode: "spots",
      spot_slugs: ["jeffreys-bay"],
      region_groups: ["Africa"],
      direct_only: true,
      max_layovers: 2,
      preferred_airlines: "ua, qf",
      max_price: 850,
    });
    expect(body.name).toBe("J-Bay");
    expect(body.destinations).toEqual({ spot_slugs: ["jeffreys-bay"], region_groups: [], countries: [] });
    expect(body.travel.max_layovers).toBe(0);
    expect(body.travel.preferred_airlines).toEqual(["UA", "QF"]);
    expect(body.travel.max_price).toBe("850.00");
    expect(body.date_start).toBeNull();
  });
});

describe("api helpers", () => {
  it("reads cookies", () => {
    expect(readCookie("sta_csrf", "a=1; sta_csrf=tok%20en; b=2")).toBe("tok en");
    expect(readCookie("missing", "a=1")).toBeNull();
  });

  it("maps field errors", () => {
    const e = new ApiError(422, "validation_error", "bad", [{ field: "travel.max_price", message: "too low" }]);
    expect(e.fieldErrors()).toEqual({ max_price: "too low" });
  });
});
