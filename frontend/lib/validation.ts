import { z } from "zod";

import type { Search, SearchInput } from "@/types/api";

export const registerSchema = z
  .object({
    full_name: z
      .string()
      .trim()
      .min(1, { message: "Enter your name." })
      .max(120)
      .refine((v) => !/[<>]/.test(v), { message: "Name must not contain < or >." }),
    email: z.email({ message: "Enter a valid email address." }),
    password: z
      .string()
      .min(10, { message: "Use at least 10 characters." })
      .max(128)
      .refine((v) => /[A-Za-z]/.test(v) && /[^A-Za-z]/.test(v), {
        message: "Include letters and at least one number or symbol.",
      }),
    confirm_password: z.string(),
  })
  .refine((v) => v.password === v.confirm_password, { message: "Passwords do not match.", path: ["confirm_password"] });

export type RegisterValues = z.infer<typeof registerSchema>;

/** Only allow same-site relative paths after login (prevents open redirects). */
export function safeNext(next: string | null | undefined): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/dashboard";
}

// ---------------------------------------------------------------- search wizard
export type WizardState = {
  name: string;
  units: "ft" | "m";
  wave_min: number;
  wave_max: number;
  wave_preferred: number | null;
  min_quality: "fair" | "good" | "very_good" | "excellent" | "exceptional";
  min_period_s: number | null;
  wind_requirement: "any" | "not_onshore" | "offshore";
  max_wind_kmh: number | null;
  swell_direction_min: number | null;
  swell_direction_max: number | null;
  min_consistency: number | null;
  break_types: string[];
  min_window_hours: number;
  destination_mode: "all" | "regions" | "spots";
  spot_slugs: string[];
  region_groups: string[];
  countries: string[];
  max_transfer_minutes: number | null;
  date_mode: "flexible" | "fixed";
  date_start: string;
  date_end: string;
  horizon_days: number;
  arrival_buffer_days: number;
  departure_buffer_days: number;
  min_days_at_destination: number;
  max_trip_days: number;
  origins: string[];
  max_origin_ground_km: number;
  max_price: number;
  currency: string;
  travelers: number;
  cabin_class: "economy" | "premium_economy" | "business" | "first";
  direct_only: boolean;
  max_layovers: number;
  max_flight_hours: number;
  preferred_airlines: string;
  priority: "balanced" | "best_waves" | "cheapest" | "shortest";
  notification_channel: "email" | "sms" | "both" | "dashboard";
  notify_surf_only: boolean;
  notify_on_updates: boolean;
};

export const WIZARD_STEPS = [
  { key: "surf", title: "Surf preferences" },
  { key: "destinations", title: "Destinations" },
  { key: "dates", title: "Travel availability" },
  { key: "flights", title: "Flight budget" },
  { key: "notifications", title: "Notifications" },
  { key: "review", title: "Review & activate" },
] as const;

export type StepKey = (typeof WIZARD_STEPS)[number]["key"];

export function defaultWizardState(homeAirport?: string | null, units: "ft" | "m" = "ft"): WizardState {
  return {
    name: "",
    units,
    wave_min: units === "m" ? 1.5 : 5,
    wave_max: units === "m" ? 4.5 : 15,
    wave_preferred: null,
    min_quality: "good",
    min_period_s: null,
    wind_requirement: "any",
    max_wind_kmh: null,
    swell_direction_min: null,
    swell_direction_max: null,
    min_consistency: null,
    break_types: [],
    min_window_hours: 4,
    destination_mode: "all",
    spot_slugs: [],
    region_groups: [],
    countries: [],
    max_transfer_minutes: null,
    date_mode: "flexible",
    date_start: "",
    date_end: "",
    horizon_days: 30,
    arrival_buffer_days: 2,
    departure_buffer_days: 1,
    min_days_at_destination: 3,
    max_trip_days: 14,
    origins: homeAirport ? [homeAirport] : [],
    max_origin_ground_km: 0,
    max_price: 850,
    currency: "USD",
    travelers: 1,
    cabin_class: "economy",
    direct_only: false,
    max_layovers: 2,
    max_flight_hours: 30,
    preferred_airlines: "",
    priority: "balanced",
    notification_channel: "email",
    notify_surf_only: false,
    notify_on_updates: true,
  };
}

export function fromSearch(s: Search): WizardState {
  return {
    name: s.name,
    units: s.units === "m" ? "m" : "ft",
    wave_min: s.wave_min,
    wave_max: s.wave_max,
    wave_preferred: s.wave_preferred ?? null,
    min_quality: s.min_quality as WizardState["min_quality"],
    min_period_s: s.min_period_s ?? null,
    wind_requirement: s.wind_requirement,
    max_wind_kmh: s.max_wind_kmh ?? null,
    swell_direction_min: s.swell_direction_min ?? null,
    swell_direction_max: s.swell_direction_max ?? null,
    min_consistency: s.min_consistency ?? null,
    break_types: [...s.break_types],
    min_window_hours: s.min_window_hours,
    destination_mode: s.destination_mode,
    spot_slugs: [...(s.destinations.spot_slugs ?? [])],
    region_groups: [...(s.destinations.region_groups ?? [])],
    countries: [...(s.destinations.countries ?? [])],
    max_transfer_minutes: s.max_transfer_minutes ?? null,
    date_mode: s.date_mode,
    date_start: s.date_start ?? "",
    date_end: s.date_end ?? "",
    horizon_days: s.horizon_days,
    arrival_buffer_days: s.travel.arrival_buffer_days ?? 2,
    departure_buffer_days: s.travel.departure_buffer_days ?? 1,
    min_days_at_destination: s.travel.min_days_at_destination ?? 3,
    max_trip_days: s.travel.max_trip_days ?? 14,
    origins: [...s.origins],
    max_origin_ground_km: s.max_origin_ground_km,
    max_price: Number(s.travel.max_price),
    currency: s.travel.currency ?? "USD",
    travelers: s.travel.travelers ?? 1,
    cabin_class: s.travel.cabin_class ?? "economy",
    direct_only: s.travel.direct_only ?? false,
    max_layovers: s.travel.max_layovers ?? 2,
    max_flight_hours: s.travel.max_flight_hours ?? 30,
    preferred_airlines: (s.travel.preferred_airlines ?? []).join(", "),
    priority: s.priority,
    notification_channel: s.notification_channel,
    notify_surf_only: s.notify_surf_only,
    notify_on_updates: s.notify_on_updates,
  };
}

export function toSearchInput(w: WizardState): SearchInput {
  return {
    name: w.name.trim(),
    date_mode: w.date_mode,
    date_start: w.date_mode === "fixed" ? w.date_start : null,
    date_end: w.date_mode === "fixed" ? w.date_end : null,
    horizon_days: w.horizon_days,
    units: w.units,
    wave_min: w.wave_min,
    wave_max: w.wave_max,
    wave_preferred: w.wave_preferred,
    min_quality: w.min_quality,
    min_period_s: w.min_period_s,
    max_wind_kmh: w.max_wind_kmh,
    wind_requirement: w.wind_requirement,
    swell_direction_min: w.swell_direction_min,
    swell_direction_max: w.swell_direction_max,
    min_consistency: w.min_consistency,
    break_types: w.break_types as SearchInput["break_types"],
    min_window_hours: w.min_window_hours,
    destination_mode: w.destination_mode,
    destinations: {
      spot_slugs: w.destination_mode === "spots" ? w.spot_slugs : [],
      region_groups: w.destination_mode === "regions" ? w.region_groups : [],
      countries: w.destination_mode === "regions" ? w.countries : [],
    },
    max_transfer_minutes: w.max_transfer_minutes,
    origins: w.origins,
    max_origin_ground_km: w.max_origin_ground_km,
    notification_channel: w.notification_channel,
    notify_surf_only: w.notify_surf_only,
    notify_on_updates: w.notify_on_updates,
    priority: w.priority,
    travel: {
      max_price: w.max_price.toFixed(2),
      currency: w.currency,
      direct_only: w.direct_only,
      max_layovers: w.direct_only ? 0 : w.max_layovers,
      max_flight_hours: w.max_flight_hours,
      min_days_at_destination: w.min_days_at_destination,
      max_trip_days: w.max_trip_days,
      travelers: w.travelers,
      preferred_airlines: w.preferred_airlines
        .split(/[\s,]+/)
        .map((c) => c.trim().toUpperCase())
        .filter(Boolean),
      cabin_class: w.cabin_class,
      arrival_buffer_days: w.arrival_buffer_days,
      departure_buffer_days: w.departure_buffer_days,
    },
  } as SearchInput;
}

/** Per-step validation mirroring the backend rules, so users see errors before submitting. */
export function validateStep(step: StepKey, w: WizardState, today: Date = new Date()): Record<string, string> {
  const e: Record<string, string> = {};
  if (step === "surf" || step === "review") {
    if (!(w.wave_min >= 0)) e.wave_min = "Enter a minimum height.";
    if (!(w.wave_max > w.wave_min)) e.wave_max = "Maximum must be greater than the minimum.";
    if (w.wave_preferred != null && (w.wave_preferred < w.wave_min || w.wave_preferred > w.wave_max))
      e.wave_preferred = "Preferred height must be within your range.";
    if ((w.swell_direction_min == null) !== (w.swell_direction_max == null))
      e.swell_direction_max = "Set both swell direction bounds or neither.";
    if (w.min_window_hours < 1) e.min_window_hours = "At least 1 hour.";
  }
  if (step === "destinations" || step === "review") {
    if (w.destination_mode === "spots" && w.spot_slugs.length === 0) e.spot_slugs = "Pick at least one spot.";
    if (w.destination_mode === "regions" && w.region_groups.length + w.countries.length === 0)
      e.region_groups = "Pick at least one region or country.";
  }
  if (step === "dates" || step === "review") {
    if (w.date_mode === "fixed") {
      if (!w.date_start || !w.date_end) e.date_start = "Choose both dates.";
      else if (w.date_end < w.date_start) e.date_end = "End date must be after the start date.";
      else {
        const yesterday = new Date(today.getTime() - 86_400_000).toISOString().slice(0, 10);
        if (w.date_start < yesterday) e.date_start = "Start date must not be in the past.";
      }
    } else if (w.horizon_days < 1 || w.horizon_days > 365) e.horizon_days = "Between 1 and 365 days.";
    if (w.max_trip_days < w.min_days_at_destination) e.max_trip_days = "Must be at least the minimum days at destination.";
  }
  if (step === "flights" || step === "review") {
    if (w.origins.length === 0) e.origins = "Add at least one departure airport.";
    if (!(w.max_price > 0)) e.max_price = "Enter a budget greater than zero.";
    if (w.travelers < 1 || w.travelers > 9) e.travelers = "Between 1 and 9 travellers.";
    const bad = w.preferred_airlines
      .split(/[\s,]+/)
      .filter(Boolean)
      .filter((c) => !/^[A-Za-z0-9]{2}$/.test(c));
    if (bad.length) e.preferred_airlines = `Use 2-character airline codes (invalid: ${bad.join(", ")}).`;
  }
  if (step === "review" && !w.name.trim()) e.name = "Give this search a name.";
  return e;
}
