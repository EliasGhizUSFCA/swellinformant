/**
 * Friendly aliases over the types generated from the backend's OpenAPI schema
 * (`npm run gen:api` after exporting backend/openapi.json). Keeping the frontend on the
 * generated types means a backend contract change surfaces as a TypeScript error.
 */
import type { components } from "./openapi";

type S = components["schemas"];

/**
 * The forecast-confidence class for responses whose API contract does not carry it yet
 * (opportunities and notifications, below; swell events, daily summaries and forecast hours
 * have their confidence fields in the generated schema). An absent field means the API does
 * not provide it; null means "not assessed yet". Classes are "high" | "moderate" | "low".
 */
export interface ForecastConfidenceFields {
  forecast_confidence_class?: string | null;
}

export type User = S["UserOut"];
export type AuthResponse = S["AuthOut"];
export type Profile = S["ProfileOut"];
export type ProfileInput = S["ProfileIn"];
export type NotificationPrefs = S["NotificationPrefsOut"];
export type NotificationPrefsInput = S["NotificationPrefsIn"];
export type SavedAirport = S["UserAirportOut"];
export type Airport = S["AirportOut"];
export type Region = S["RegionOut"];

export type SpotMini = S["SpotMini"];
export type SpotSummary = S["SpotSummary"];
export type SpotDetail = S["SpotDetail"];
export type SpotAirport = S["SpotAirportOut"];
export type Conditions = S["Conditions"];
export type SwellEvent = S["EventOut"];
export type SpotForecast = S["SpotForecastOut"];
export type ForecastPoint = S["ForecastPoint"];
export type DailySummary = S["DailySummary"];
export type MapSnapshot = S["MapSnapshotOut"];
export type MapSpot = S["MapSpotOut"];
export type ModelSeries = S["ModelSeries"];
export type ModelPoint = S["ModelPoint"];
export type EnsemblePoint = S["EnsemblePoint"];
export type BlendInfo = S["BlendInfo"];
export type PhysicalProfile = S["PhysicalProfileOut"];
export type Research = S["ResearchOut"];
export type ResearchVariable = S["ResearchVariableOut"];
export type ResearchSource = S["ResearchSourceOut"];

// The unified spot timeline (GET /api/spots/{slug}/timeline) and its tide / observation blocks.
export type SpotTimeline = S["SpotTimelineOut"];
export type TimelineStep = S["TimelineStepOut"];
export type TimelineDay = S["TimelineDayOut"];
export type PartitionOut = S["PartitionOut"];
export type TideBlock = S["TideBlockOut"];
export type ObservationsBlock = S["ObservationsBlockOut"];

export type SearchInput = S["SearchIn"];
export type Search = S["SearchOut"];
export type TravelInput = S["TravelIn"];

// Opportunities and alerts do not carry a forecast confidence class in the API contract yet;
// the optional field lets the UI show it as soon as the backend sends it (and nothing until then).
export type MatchSummary = S["MatchSummary"] & Pick<ForecastConfidenceFields, "forecast_confidence_class">;
export type MatchDetail = Omit<S["MatchDetail"], "event" | "daily"> &
  Pick<ForecastConfidenceFields, "forecast_confidence_class"> & { event: SwellEvent; daily: DailySummary[] };
export type Offer = S["OfferOut"];
export type FlightSlice = S["SliceOut"];
export type NotificationItem = S["NotificationOut"] & Pick<ForecastConfidenceFields, "forecast_confidence_class">;
export type NotificationPage = Omit<S["NotificationPage"], "items"> & { items: NotificationItem[] };
export type VerificationStatus = S["VerificationStatusOut"];
export type VerifyResult = S["VerifyOut"];

export type QualityLabel = "poor" | "fair" | "good" | "very_good" | "excellent" | "exceptional";

export interface SystemStatus {
  mode: {
    demo_mode: boolean;
    forecast_providers: string[];
    flight_provider: string;
    email_provider: string;
    email_delivery: "capture" | "live";
    dev_inbox_url: string | null;
    captured_email_location: string | null;
    sms_provider: string;
    dev_tools: boolean;
  };
  detection: { lead_min_days: number; lead_max_days: number; event_min_score: number };
  forecast: {
    stale_after_hours: number;
    sources: Array<{
      code: string;
      name: string;
      is_demo: boolean;
      stale: boolean;
      latest_successful: null | { run_key: string; issued_at: string; status: string; spot_count: number };
      latest_attempt: null | { run_key: string; status: string; error: string | null };
    }>;
  };
  events: Record<string, number>;
  jobs: Array<{ job: string; status: string; started_at: string | null; duration_ms: number | null }>;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: Array<{ field: string; message: string }> };
}

export type AccuracyOverview = S["AccuracyOverview"];
export type AccuracyMetric = S["MetricRow"];
export type AccuracyPairs = S["PairsOut"];
export type ModelVersionSummary = S["ModelVersionSummary"];
