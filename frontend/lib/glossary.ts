/**
 * Plain-language explanations of technical terms, shown in "Learn more" popovers. Each entry
 * restates what the app actually computes (docs/SURF_SCORING.md); keep them in sync.
 */
import { FORECAST_CONFIDENCE_MEANING, LOCAL_CONFIDENCE_MEANING, RESEARCH_CONFIDENCE_MEANING } from "@/lib/confidence";
import { GROUNDSWELL_PERIOD_S } from "@/lib/rating";

export interface GlossaryEntry {
  title: string;
  body: string;
}

export const GLOSSARY = {
  offshoreHeight: {
    title: "Offshore wave height",
    body: "The significant wave height of the open sea at the forecast model's grid point offshore of the break. It is not the surf size: waves change a lot as they reach shallow water.",
  },
  breakingHeight: {
    title: "Breaking wave height (our estimate)",
    body: "Our estimate of the wave faces (trough to crest) where the waves break at this spot, worked out from the offshore swell, its period and direction and the break's exposure. “Typical” is the average breaking wave; “sets” are the bigger ones (about 1.27 × typical). It is a model estimate, not a measurement.",
  },
  range80: {
    title: "80% range",
    body: "The range our error model expects to contain the real breaking height about 8 times in 10. That error model is an assumption (based on lead time or ensemble spread) that has not been checked against surf observations yet, so treat the range as a guide.",
  },
  groundswell: {
    title: "Groundswell",
    body: `Swell from distant storms that has travelled far from where it formed. Long periods (here ${GROUNDSWELL_PERIOD_S} s or more) carry more energy and usually give more powerful, organised surf than short-period swell.`,
  },
  ensemble: {
    title: "Ensemble",
    body: "A set of forecasts run from slightly different starting conditions (here NOAA's GEFS-Wave). When the members agree the forecast is more settled; a wide spread means more uncertainty.",
  },
  modelComparison: {
    title: "Why models differ",
    body: "Each line is one numerical model's raw output at the offshore point. These are open-water values, not surf heights. Models legitimately disagree; their spread is one input to forecast confidence, not an error.",
  },
  qualityRating: {
    title: "Surf quality rating",
    body: "A 0–10 rating (our 0–100 score divided by ten) that combines swell direction, period, size, wind, tide and consistency for this break with documented weights. It ranks conditions; it is not a probability or a measurement.",
  },
  windRelation: {
    title: "Offshore and onshore wind",
    body: "Offshore wind blows from the land towards the sea and grooms the waves; onshore wind blows in from the sea and makes them choppy. Cross-shore is in between. Judged against this break's offshore direction.",
  },
  forecastConfidence: { title: "Forecast confidence", body: FORECAST_CONFIDENCE_MEANING },
  localConfidence: { title: "Confidence at the break", body: LOCAL_CONFIDENCE_MEANING },
  researchConfidence: { title: "Spot research confidence", body: RESEARCH_CONFIDENCE_MEANING },
  swellWindow: {
    title: "Swell window",
    body: "The range of directions (compass degrees the swell comes FROM) from which swell can reach the break without being blocked by land or reefs.",
  },
  overallMatch: {
    title: "Overall match",
    body: "Combines surf quality, price, a forecast confidence index and trip convenience into one ranking number, weighted by your search's priority. It is a heuristic index, not a probability.",
  },
} satisfies Record<string, GlossaryEntry>;

export type GlossaryKey = keyof typeof GLOSSARY;
