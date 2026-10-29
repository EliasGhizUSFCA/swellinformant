import { relativeTime } from "@/lib/format";
import { cycleLabel } from "@/lib/map-time";
import type { RunMember } from "@/types/api";

/** The wave and wind (atmosphere) model runs of a forecast whose components come from
 * different runs: a refresh of one component keeps the other's original run (the API then
 * lists one `run.members` entry per component). */
export type ComponentRuns = { wave: RunMember; atmosphere: RunMember };

/** The per-component entries of `members`, or null when the forecast's wave and wind data
 * share one model run (or the API names no component). */
export function componentRuns(members: readonly RunMember[] | null | undefined): ComponentRuns | null {
  const wave = members?.find((m) => m.component === "wave" && m.init_time);
  const atmosphere = members?.find((m) => m.component === "atmosphere" && m.init_time);
  return wave && atmosphere ? { wave, atmosphere } : null;
}

/** "Wave run 07Z Oct 29 · Wind run 01Z Oct 29". */
export function componentRunsText(runs: ComponentRuns): string {
  return `Wave run ${cycleLabel(runs.wave.init_time!)} · Wind run ${cycleLabel(runs.atmosphere.init_time!)}`;
}

/** When each component's data were fetched: one time when both were fetched together (within
 * a minute), else "waves 19 minutes ago · wind 6 hours ago". Null when unknown. */
export function componentFetchedText(runs: ComponentRuns, now: Date): string | null {
  const wave = runs.wave.retrieved_at;
  const wind = runs.atmosphere.retrieved_at;
  if (!wave || !wind) return null;
  if (Math.abs(Date.parse(wave) - Date.parse(wind)) < 60_000) return relativeTime(wave, now);
  return `waves ${relativeTime(wave, now)} · wind ${relativeTime(wind, now)}`;
}

/** Whole hours from the wave model run's init time to `t` (epoch ms), or null. */
export function hoursAfterWaveRun(runs: ComponentRuns, t: number): number | null {
  const init = Date.parse(runs.wave.init_time!);
  return Number.isFinite(init) && Number.isFinite(t) ? Math.round((t - init) / 3_600_000) : null;
}
