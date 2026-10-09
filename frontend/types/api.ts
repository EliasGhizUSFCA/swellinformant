/**
 * Friendly aliases over the types generated from the backend's OpenAPI schema
 * (`npm run gen:api` after exporting backend/openapi.json). Keeping the frontend on the
 * generated types means a backend contract change surfaces as a TypeScript error.
 */
import type { components } from "./openapi";

type S = components["schemas"];

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

export type SearchInput = S["SearchIn"];
export type Search = S["SearchOut"];
export type TravelInput = S["TravelIn"];

export type MatchSummary = S["MatchSummary"];
export type MatchDetail = S["MatchDetail"];
export type Offer = S["OfferOut"];
export type FlightSlice = S["SliceOut"];
export type NotificationItem = S["NotificationOut"];
export type NotificationPage = S["NotificationPage"];

export type QualityLabel = "poor" | "fair" | "good" | "very_good" | "excellent" | "exceptional";

export interface SystemStatus {
  mode: {
    demo_mode: boolean;
    forecast_providers: string[];
    flight_provider: string;
    email_provider: string;
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
