/**
 * Provider-specific display rules (design D3): which forecast provider a source code belongs
 * to, the credit its terms ask for, and whether its model run times are estimates.
 *
 * * Windy publishes no model run time, so the run time of Windy data is estimated (from the
 *   newest GFS cycle whose expected availability has passed) and is shown as "estimated";
 *   the published version says so per component (`wind_run_estimated`, `wave_run_estimated`),
 *   which wins whenever the page has it.
 * * Windy's terms ask for the credit "Contains data from the Windy database" with a link to
 *   windy.com wherever Windy data is shown (the logo is an owner step, docs/WINDY_API_SETUP.md).
 * * Open-Meteo's credit and CC BY 4.0 licence links come from the API (`attribution_links`)
 *   and are shown as sent.
 */
import type { AttributionLink } from "@/types/api";

export const WINDY_CREDIT = "Contains data from the Windy database";
export const WINDY_URL = "https://www.windy.com/";

/** Forecast source codes of the Windy adapter (e.g. "windy_gfswave"). */
export function isWindySource(code: string | null | undefined): boolean {
  return typeof code === "string" && code.toLowerCase().startsWith("windy");
}

function isWindyLink(href: string): boolean {
  try {
    const host = new URL(href).hostname.toLowerCase();
    return host === "windy.com" || host.endsWith(".windy.com");
  } catch {
    return false;
  }
}

/** Whether a model run time is an estimate: the version's own flag when known, else true for
 * Windy sources (no run time is published), else false. */
export function runTimeEstimated(sourceCode: string | null | undefined, versionFlag?: boolean | null): boolean {
  if (typeof versionFlag === "boolean") return versionFlag;
  return isWindySource(sourceCode);
}

/**
 * The credit links to show: the API's links with Windy's named by the credit its terms ask
 * for, plus that credit when the data is Windy's (`sourceCode`) and no Windy link was sent.
 * Every other link (Open-Meteo, CC BY 4.0, ECMWF, NOAA) is kept as sent, in order.
 */
export function creditLinks(links: readonly AttributionLink[] | null | undefined, sourceCode?: string | null): AttributionLink[] {
  const out = (links ?? []).map((l) => (isWindyLink(l.href) ? { ...l, label: WINDY_CREDIT } : l));
  if (isWindySource(sourceCode) && !out.some((l) => isWindyLink(l.href))) out.push({ label: WINDY_CREDIT, href: WINDY_URL });
  const seen = new Set<string>();
  return out.filter((l) => {
    const k = `${l.label}|${l.href}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
