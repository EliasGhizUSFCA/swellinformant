/**
 * SYNTHETIC API responses for e2e/confidence.spec.ts. Every spot, swell, reason, source and
 * fare below is invented for the test, labelled "Synthetic" / "test fixture" where it is
 * shown, and served only through page.route mocks. None of it is real data.
 */
import type {
  Conditions,
  DailySummary,
  ForecastPoint,
  MatchDetail,
  MatchSummary,
  NotificationPage,
  Research,
  SpotDetail,
  SpotForecast,
  SwellEvent,
  User,
} from "../types/api";

export const SPOT_SLUG = "synthetic-point";
export const MATCH_ID = "00000000-0000-4000-8000-00000000c0f1";
const TZ = "UTC";
const HOUR = 3_600_000;

export interface Scenario {
  name: string;
  forecastClass: "high" | "moderate" | "low" | null;
  localClass: "high" | "moderate" | "low" | null;
  /** Forecast-confidence reasons (the API's forecast_confidence_reasons: forecast rules only). */
  reasons: string[];
  /** Why confidence at the break is lower (local_confidence_reason); null when the classes match. */
  localReason: string | null;
  /** The disputed-wind note (wind_dispute_note), when the sources disagree. */
  windNote?: string | null;
  research: Research | null;
}

/** The deprecated mixed confidence_reasons list, built the way the backend builds it: the
 * forecast reasons, then the disputed-wind note, then the local (research) reason. */
function mixedReasons(sc: Scenario): string[] {
  return [...sc.reasons, ...(sc.windNote ? [sc.windNote] : []), ...(sc.localReason ? [sc.localReason] : [])];
}

/** The combined "local confidence" the API sent before 2.0 removed it (spec 20). The mocks
 * still send it, as a response cached before the upgrade could, to prove the UI ignores it. */
function legacyLocal(sc: Scenario, withReason = true) {
  return withReason
    ? { local_confidence_class: sc.localClass, local_confidence_reason: sc.localReason }
    : { local_confidence_class: sc.localClass };
}

/** The wind fields the API adds from its wind model (synthetic speeds; the backend's classes). */
function windFields(kmh: number) {
  const strength = kmh <= 5 ? "glassy" : kmh < 12 ? "light" : kmh < 29 ? "moderate" : "strong";
  return { wind_strength: strength, wind_favourability: "favourable" } as const;
}

const SOURCES: Research["sources"] = [
  {
    title: "Synthetic Surf Guide (test fixture)",
    url: "https://example.org/synthetic-surf-guide",
    publisher: "Example Test Press",
    source_type: "guidebook",
    published_year: 2021,
    accessed_at: "2026-09-15T00:00:00Z",
  },
  {
    title: "Synthetic harbour notice (test fixture)",
    url: "https://example.org/synthetic-harbour-notice",
    publisher: "Example Test Agency",
    source_type: "official",
    published_year: 2019,
    accessed_at: "2026-09-15T00:00:00Z",
  },
];

export const SCENARIOS = {
  highResearchLowForecast: {
    name: "high research + low forecast",
    forecastClass: "low",
    localClass: "low",
    reasons: [
      "Synthetic reason: the swell forecast is about seven days away, so conditions may change.",
      "Synthetic reason: the forecast models disagree on the swell size.",
    ],
    localReason: null,
    research: {
      research_class: "high",
      summary: "Synthetic summary: the break's preferred swell direction, tide and wind are supported by multiple test sources.",
      gaps: [],
      variables: [
        { variable: "swell_direction", label: "Swell direction", research_class: "high", summary: "Synthetic: two test sources agree.", gaps: [], source_count: 2 },
        { variable: "wind", label: "Wind", research_class: "high", summary: "Synthetic: two test sources agree.", gaps: [], source_count: 2 },
        { variable: "tide", label: "Tide", research_class: "moderate", summary: "Synthetic: one test source.", gaps: [], source_count: 1 },
      ],
      sources: SOURCES,
      methodology_version: "synthetic-test-v1",
      assessed_at: "2026-10-01T00:00:00Z",
      research_updated_at: "2026-09-30T00:00:00Z",
    },
  },
  lowResearchHighForecast: {
    name: "low research + high forecast",
    forecastClass: "high",
    localClass: "moderate",
    reasons: ["Synthetic reason: the swell is about two days away and the models agree."],
    // The backend's own sentence for a Moderate research basis (forecast_confidence.RESEARCH_REASONS):
    // local reasons are always one of these, and the mixed list carries it too.
    localReason: "How this break responds to swell is only partly documented.",
    research: {
      research_class: "low",
      summary: "Synthetic summary: only one test source describes this break.",
      gaps: [
        "Synthetic gap: no test source describes the preferred tide.",
        "Synthetic gap: test sources disagree on the best swell direction.",
      ],
      variables: [
        {
          variable: "swell_direction",
          label: "Swell direction",
          research_class: "low",
          summary: "Synthetic: test sources disagree.",
          gaps: ["Synthetic gap: test sources disagree on the best swell direction."],
          source_count: 1,
        },
        { variable: "tide", label: "Tide", research_class: "low", summary: "Synthetic: not documented.", gaps: ["Synthetic gap: no test source describes the preferred tide."], source_count: 0 },
      ],
      sources: SOURCES.slice(0, 1),
      methodology_version: "synthetic-test-v1",
      assessed_at: "2026-10-01T00:00:00Z",
      research_updated_at: null,
    },
  },
  notAssessed: { name: "not assessed", forecastClass: null, localClass: null, reasons: [], localReason: null, research: null },
} satisfies Record<string, Scenario>;

function iso(offsetHours: number): string {
  const t = Math.floor(Date.now() / HOUR) * HOUR + offsetHours * HOUR;
  return new Date(t).toISOString();
}

/** A synthetic swell that peaks about seven days out (smooth bump, no real data). */
function wave(h: number) {
  const bump = Math.exp(-(((h - 168) / 30) ** 2));
  return { hs: 0.8 + 1.6 * bump, period: 9 + 7 * bump, wind: 8 + 10 * (1 - bump) };
}

function scoreAt(h: number): number {
  return Math.round(20 + 69 * Math.exp(-(((h - 168) / 30) ** 2)));
}

function labelFor(score: number): string {
  return score >= 95 ? "exceptional" : score >= 85 ? "excellent" : score >= 72 ? "very_good" : score >= 57 ? "good" : score >= 40 ? "fair" : "poor";
}

function point(h: number, sc: Scenario): ForecastPoint {
  const w = wave(h);
  const score = scoreAt(h);
  const hb = 2 + 6 * Math.exp(-(((h - 168) / 30) ** 2));
  const hour = new Date(iso(h)).getUTCHours();
  return {
    valid_time: iso(h),
    is_daylight: hour >= 6 && hour < 18,
    sig_wave_height_m: Math.round(w.hs * 10) / 10 + 0.2,
    wave_period_s: Math.round(w.period),
    wave_period_kind: "tp",
    wave_direction_deg: 220,
    primary_swell_height_m: Math.round(w.hs * 10) / 10,
    primary_swell_period_s: Math.round(w.period),
    primary_swell_direction_deg: 220,
    secondary_swell_height_m: 0.4,
    secondary_swell_period_s: 8,
    secondary_swell_direction_deg: 180,
    wind_wave_height_m: 0.2,
    wind_wave_period_s: 4,
    wind_speed_kmh: Math.round(w.wind),
    wind_direction_deg: 30,
    wind_gust_kmh: Math.round(w.wind * 1.4),
    pressure_msl_hpa: 1016,
    sea_level_m: null,
    wave_power_kw_m: 12,
    breaking_height_min_ft: Math.round(hb * 2) / 2,
    breaking_height_max_ft: Math.round(hb * 1.27 * 2) / 2,
    breaking_height_mid_ft: hb,
    breaking_p10_ft: Math.round(hb * 0.6 * 2) / 2,
    breaking_p90_ft: Math.round(hb * 1.6 * 2) / 2,
    uncertainty_kind: "heuristic",
    breaking_height_method: "linear_wave_v2",
    algorithm_version: "2.1.0",
    ml_model_version: null,
    score,
    label: labelFor(score),
    confidence: 61,
    confidence_label: "moderate",
    wind_relation: "offshore",
    explanation: "Synthetic explanation: swell well aligned with the break; offshore wind (test fixture).",
    components: { swell_direction: 0.95, swell_period: Math.min(1, w.period / 15), wave_size: 0.8, wind: 0.9, consistency: 0.8, size_gate: 1, period_gate: 1, wind_gate: 1 },
    forecast_confidence_class: sc.forecastClass,
    ...legacyLocal(sc),
    confidence_reasons: mixedReasons(sc),
    forecast_confidence_reasons: sc.reasons,
    wind_dispute_note: sc.windNote ?? null,
    lead_hours: h,
    ...windFields(Math.round(w.wind)),
  };
}

function pick(o: Record<string, unknown>, keys: string[]): Record<string, unknown> {
  return Object.fromEntries(keys.filter((k) => k in o).map((k) => [k, o[k]]));
}

function toConditions(p: ForecastPoint): Conditions {
  return {
    valid_time: p.valid_time,
    score: p.score,
    label: p.label,
    breaking_height_min_ft: p.breaking_height_min_ft,
    breaking_height_max_ft: p.breaking_height_max_ft,
    breaking_height_mid_ft: p.breaking_height_mid_ft ?? null,
    breaking_p10_ft: p.breaking_p10_ft ?? null,
    breaking_p90_ft: p.breaking_p90_ft ?? null,
    uncertainty_kind: p.uncertainty_kind ?? null,
    swell_height_m: p.primary_swell_height_m,
    swell_period_s: p.primary_swell_period_s,
    swell_direction_deg: p.primary_swell_direction_deg,
    wind_speed_kmh: p.wind_speed_kmh,
    wind_direction_deg: p.wind_direction_deg,
    wind_relation: p.wind_relation,
    confidence: p.confidence,
    confidence_label: p.confidence_label,
    is_daylight: p.is_daylight,
    sig_wave_height_m: p.sig_wave_height_m,
    source_code: "demo",
    source_name: "Synthetic test forecast (not real data)",
    is_demo: true,
    run_issued_at: iso(-6),
    method: "linear_wave_v2",
    algorithm_version: "2.1.0",
    explanation: p.explanation,
    forecast_confidence_class: p.forecast_confidence_class ?? null,
    // Whatever legacy combined class the mocked point carries (see legacyLocal).
    ...pick(p as Record<string, unknown>, ["local_confidence_class", "local_confidence_reason"]),
    confidence_reasons: p.confidence_reasons ?? [],
    forecast_confidence_reasons: p.forecast_confidence_reasons ?? [],
    wind_dispute_note: p.wind_dispute_note ?? null,
    wind_strength: p.wind_strength ?? null,
    wind_favourability: p.wind_favourability ?? null,
    lead_hours: p.lead_hours ?? null,
  };
}

const SPOT_MINI = {
  id: 9001,
  slug: SPOT_SLUG,
  name: "Synthetic Point (test fixture)",
  country: "Testland",
  country_code: "TL",
  region: "Fixture Coast",
  region_group: "Synthetic",
  latitude: -34.05,
  longitude: 24.93,
  timezone: TZ,
  break_type: "point",
};

/** The peak hour is 168 h (seven days) out; it is the spot's best upcoming hour. */
const PEAK_H = 168;

export function spotDetail(sc: Scenario): SpotDetail {
  return {
    ...SPOT_MINI,
    wave_direction: "right",
    is_big_wave: false,
    skill_level: "intermediate",
    wave_height_min_ft: 3,
    wave_height_max_ft: 12,
    best_months: [6, 7, 8],
    primary_airport: null,
    current: toConditions(point(0, sc)),
    best_upcoming: toConditions(point(PEAK_H, sc)),
    next_event: null,
    upcoming_event_count: 1,
    research_class: sc.research?.research_class ?? null,
    forecast_latitude: -34.1,
    forecast_longitude: 24.95,
    swell_window_min: 150,
    swell_window_max: 270,
    optimal_swell_direction_min: 200,
    optimal_swell_direction_max: 240,
    offshore_wind_direction: 30,
    min_swell_period_s: 10,
    ideal_swell_period_s: 15,
    tide_preference: null,
    height_factor: 1,
    calibrated: false,
    seasonality: "Synthetic test fixture: no real season.",
    accessibility_notes: "Synthetic test fixture: no real access.",
    hazards: "Synthetic test fixture: no real hazards are described.",
    description: "SYNTHETIC TEST FIXTURE — not a real surf spot. Used only to check how confidence is displayed.",
    airports: [],
    physical_profile: null,
    research: sc.research,
  };
}

function daily(sc: Scenario): DailySummary[] {
  return Array.from({ length: 10 }, (_, d) => {
    const h = d * 24 + 12;
    const p = point(h, sc);
    return {
      date: iso(h).slice(0, 10),
      best_score: p.score,
      best_label: p.label,
      best_time: p.valid_time,
      height_min_ft: p.breaking_height_min_ft,
      height_max_ft: p.breaking_height_max_ft,
      avg_wind_kmh: p.wind_speed_kmh,
      good_hours: p.score >= 57 ? 8 : 0,
      confidence: 61,
      forecast_confidence_class: sc.forecastClass,
      ...legacyLocal(sc, false),
    };
  });
}

export function spotForecast(sc: Scenario): SpotForecast {
  return {
    spot_slug: SPOT_SLUG,
    timezone: TZ,
    available: true,
    message: null,
    source: { code: "demo", name: "Synthetic test forecast (not real data)", is_demo: true, attribution: "Synthetic test fixture.", wave_model: "synthetic", atmosphere_model: "synthetic", attribution_links: [] },
    run: {
      run_key: "synthetic-test",
      issued_at: iso(-6),
      completed_at: iso(-5),
      wave_model_run_at: iso(-6),
      atmosphere_model_run_at: iso(-6),
      status: "success",
      members: [
        { code: "demo", name: "Synthetic test forecast (not real data)", family: "synthetic", run_key: "synthetic-test", init_time: iso(-6), wave_model: "synthetic", atmosphere_model: "synthetic" },
      ],
    },
    stale: false,
    breaking_height_method: "linear_wave_v2",
    algorithm_version: "2.1.0",
    calibrated: false,
    points: Array.from({ length: 80 }, (_, i) => point(i * 3, sc)),
    daily: daily(sc),
    models: [],
    ensemble: [],
    blend: null,
    alerts_paused: false,
  };
}

export function swellEvent(sc: Scenario, opts: { id: string; name: string; startH: number; score: number }): SwellEvent {
  const peak = point(opts.startH + 24, sc);
  return {
    id: opts.id,
    spot: { ...SPOT_MINI, slug: SPOT_SLUG, name: opts.name },
    status: "active",
    start_time: iso(opts.startH),
    end_time: iso(opts.startH + 48),
    peak_time: peak.valid_time,
    peak_score: opts.score,
    avg_score: opts.score - 8,
    peak_label: labelFor(opts.score),
    peak_breaking_height_min_ft: 5,
    peak_breaking_height_max_ft: 7,
    peak_swell_height_m: 2.1,
    peak_swell_period_s: 16,
    peak_swell_direction_deg: 220,
    peak_wind_speed_kmh: 8,
    peak_wind_direction_deg: 30,
    peak_wind_relation: "offshore",
    qualifying_hours: 14,
    confidence: 61,
    confidence_label: "moderate",
    lead_days: opts.startH / 24,
    version: 1,
    first_detected_at: iso(-24),
    last_updated_at: iso(-5),
    is_demo: true,
    source_code: "demo",
    history: [],
    forecast_confidence_class: sc.forecastClass,
    ...legacyLocal(sc),
    confidence_reasons: mixedReasons(sc),
    forecast_confidence_reasons: sc.reasons,
    wind_dispute_note: sc.windNote ?? null,
    attribution_links: [],
    ...windFields(8),
    stale: false,
  };
}

export function user(): User {
  return {
    id: "00000000-0000-4000-8000-0000000000u1",
    email: "synthetic-user@example.com",
    full_name: "Synthetic Tester",
    email_verified: true,
    is_admin: false,
    created_at: "2026-01-01T00:00:00Z",
    profile: {
      home_airport: "TST",
      home_city: null,
      preferred_wave_min_ft: null,
      preferred_wave_max_ft: null,
      preferred_break_type: null,
      experience_level: null,
      units: "ft",
      currency: "USD",
    },
    notification_preferences: {
      email_enabled: true,
      sms_enabled: false,
      all_paused: false,
      phone_number: null,
      phone_verified: false,
      sms_ready: false,
      sms_opted_out: false,
      max_alerts_per_day: 3,
      sms_service_available: false,
    },
    airports: [],
  };
}

export function matchSummary(sc: Scenario): MatchSummary {
  return {
    id: MATCH_ID,
    search_id: "00000000-0000-4000-8000-0000000000s1",
    search_name: "Synthetic test search",
    search_status: "active",
    status: "flight_found",
    status_reason: null,
    spot: SPOT_MINI,
    event_id: "00000000-0000-4000-8000-0000000000e1",
    event_status: "active",
    window_start: iso(150),
    window_end: iso(190),
    qualifying_hours: 14,
    peak_score: 89,
    avg_score: 80,
    peak_label: "excellent",
    breaking_height_min_ft: 5,
    breaking_height_max_ft: 7,
    confidence: 61,
    confidence_label: "moderate",
    destination_iata: "TST",
    transfer_minutes: 60,
    origin_iata: "SFO",
    recommended_arrival_date: iso(120).slice(0, 10),
    recommended_departure_date: iso(220).slice(0, 10),
    best_price: "1234",
    currency: "USD",
    overall_score: 77,
    scores: { surf: 89, affordability: 70, confidence: 61, convenience: 65 },
    is_demo: true,
    has_mock_flights: true,
    flight_checked_at: iso(-2),
    notification_count: 1,
    lead_days: 6.25,
    created_at: iso(-24),
    updated_at: iso(-2),
    forecast_confidence_class: sc.forecastClass,
    ...legacyLocal(sc),
    stale: false,
    // Required since P8 (2.0); the values the fixture always implied (undefined = false).
    short_notice: false,
    best_offer_expired: false,
  };
}

export function matchDetail(sc: Scenario): MatchDetail {
  const e = swellEvent(sc, { id: "00000000-0000-4000-8000-0000000000e1", name: SPOT_MINI.name, startH: 150, score: 89 });
  return {
    ...matchSummary(sc),
    event: e,
    spot_airports: [],
    accessibility_notes: "Synthetic test fixture.",
    daily: daily(sc).slice(4, 10),
    offers: [],
    travel_window: null,
    inspection_links: [],
    limitations: ["MOCK FARES: synthetic test fixture, not real prices.", "Breaking wave heights are estimates (synthetic test fixture)."],
    flight_search_error: null,
  };
}

export function notifications(sc: Scenario): NotificationPage {
  return {
    total: 1,
    items: [
      {
        id: "00000000-0000-4000-8000-0000000000n1",
        channel: "email",
        kind: "new_opportunity",
        status: "captured",
        subject: "Synthetic test alert: Synthetic Point (test fixture)",
        recipient_masked: "s***@example.com",
        created_at: iso(-3),
        sent_at: iso(-3),
        delivered_at: null,
        provider: "console",
        attempts: 1,
        last_error: null,
        match_id: MATCH_ID,
        match_status: "flight_found",
        event_status: "active",
        spot_name: SPOT_MINI.name,
        spot_slug: SPOT_SLUG,
        price: "1234",
        currency: "USD",
        peak_label: "excellent",
        window_label: "7 days out (synthetic)",
        is_demo: true,
        forecast_confidence_class: sc.forecastClass,
        ...legacyLocal(sc),
      },
    ],
  };
}
