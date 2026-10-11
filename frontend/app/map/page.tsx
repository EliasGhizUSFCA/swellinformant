"use client";

import "./map.css";

import { Info, LocateFixed, Loader2 } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { toast } from "sonner";

import { AppShell } from "@/components/layout/app-shell";
import type { ParticleMode } from "@/components/map/forecast-map";
import { LayerTabs } from "@/components/map/layer-tabs";
import { LegendDrawer, LegendTab } from "@/components/map/legend-drawer";
import { FOCUS, GLASS } from "@/components/map/map-ui";
import { SettingsPopover } from "@/components/map/settings-popover";
import { NO_FILTERS, passes, type Filters } from "@/components/map/spot-filters";
import { SpotList } from "@/components/map/spot-list";
import { SpotPanel } from "@/components/map/spot-panel";
import type { MarkerState } from "@/components/map/surf-map";
import { Timeline } from "@/components/map/timeline";
import { useCityAirports, useFieldGrids, useFieldIndex } from "@/hooks/map-fields";
import { useMediaQuery, usePageVisible, usePrefersReducedMotion } from "@/hooks/map-media";
import { useMapSettings } from "@/hooks/map-settings";
import { useMapConditions, useMe, useSpots } from "@/hooks/queries";
import { formatDateTime, heightRange, type Units } from "@/lib/format";
import type { FieldVariableName } from "@/lib/map-fields";
import { isobarsFor } from "@/lib/map-geo";
import { layerDef, layerScale, type DisplayUnits } from "@/lib/map-layers";
import { bubbleLabel, nearestStepIndex, timelineSteps } from "@/lib/map-time";
import { QUALITY_LABELS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { QualityLabel } from "@/types/api";

const ForecastMap = dynamic(() => import("@/components/map/forecast-map"), {
  ssr: false,
  loading: () => <div className="absolute inset-0 bg-[#151a21]" />,
});

/** A spot prediction is shown for a step only if the server found one within this window. */
const PREDICTION_WINDOW_MS = 90 * 60_000;

export default function MapPage() {
  const { data: spots = [], isLoading, isError } = useSpots();
  const { data: user } = useMe();
  const [settings, updateSettings] = useMapSettings();
  const heightUnits: Units = settings.heightUnits ?? (user?.profile.units as Units | undefined) ?? "ft";
  const units: DisplayUnits = useMemo(() => ({ height: heightUnits, wind: settings.windUnits }), [heightUnits, settings.windUnits]);
  const tz = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const reducedMotion = usePrefersReducedMotion();
  const pageVisible = usePageVisible();
  const isDesktop = useMediaQuery("(min-width: 1024px)");

  const [selected, setSelected] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [legendOpen, setLegendOpen] = useState(false);
  const [listOpen, setListOpen] = useState<boolean | null>(null);
  const [playing, setPlaying] = useState(false);
  const [zoom, setZoom] = useState(2);
  const [userLocation, setUserLocation] = useState<{ lat: number; lon: number; key: number } | null>(null);
  const [locating, setLocating] = useState(false);
  const [selectedTime, setSelectedTime] = useState<string | null>(null); // null = the server's "now"

  // --- forecast time --------------------------------------------------------------------
  const fieldIndex = useFieldIndex();
  const index = fieldIndex.data;
  const fieldsAvailable = !!index?.available && index.valid_times.length > 0;
  // The "now" snapshot (cached) defines the spot range and the server's current hour.
  const nowSnapshot = useMapConditions(null);
  const range = useMemo(() => {
    const d = nowSnapshot.data;
    return d?.valid_time && d.range_end ? { start: d.valid_time, end: d.range_end, stepHours: d.step_hours } : null;
  }, [nowSnapshot.data]);
  const steps = useMemo(() => timelineSteps(fieldsAvailable ? index.valid_times : null, range), [fieldsAvailable, index, range]);
  // Without ocean layers, step 0 is the server's "now" hour (the default snapshot); with them,
  // the valid time closest to it.
  const nowIndex = fieldsAvailable ? nearestStepIndex(steps, nowSnapshot.data?.valid_time) : 0;
  const stepIndex = selectedTime ? nearestStepIndex(steps, selectedTime) : nowIndex;
  const at = steps[stepIndex] ?? null;
  const snapshotWanted = fieldsAvailable || stepIndex > 0 ? at : null;

  const [debounced, setDebounced] = useState<{ snapshot: string | null; field: string | null }>({ snapshot: null, field: null });
  useEffect(() => {
    const t = setTimeout(() => setDebounced({ snapshot: snapshotWanted, field: fieldsAvailable ? at : null }), 250);
    return () => clearTimeout(t);
  }, [snapshotWanted, at, fieldsAvailable]);

  const snapshot = useMapConditions(debounced.snapshot);
  // keepPreviousData shows the last hour while the next loads; only a settled answer whose
  // time is far from the requested one means "no spot predictions at this time".
  const predictionsCover = useMemo(() => {
    const d = snapshot.data;
    if (!d?.valid_time || snapshot.isPlaceholderData || !debounced.snapshot) return true;
    return Math.abs(Date.parse(d.valid_time) - Date.parse(debounced.snapshot)) <= PREDICTION_WINDOW_MS;
  }, [snapshot.data, snapshot.isPlaceholderData, debounced.snapshot]);
  const bySlug = useMemo(
    () => new Map(predictionsCover ? (snapshot.data?.spots ?? []).map((m) => [m.slug, m]) : []),
    [snapshot.data, predictionsCover],
  );

  useEffect(() => {
    if (!playing) return;
    const t = setTimeout(() => {
      if (stepIndex >= steps.length - 1) setPlaying(false);
      else setSelectedTime(steps[stepIndex + 1] ?? null);
    }, 1100);
    return () => clearTimeout(t);
  }, [playing, stepIndex, steps]);

  // --- ocean layers ---------------------------------------------------------------------
  const layer = settings.layer;
  const def = layerDef(layer);
  const offered = useMemo(() => new Set((index?.variables ?? []).map((v) => v.name)), [index]);
  const layerVars = useMemo(() => def.needs.filter((v) => offered.size === 0 || offered.has(v)), [def, offered]);
  const pressureVars = useMemo<FieldVariableName[]>(() => (settings.pressure && offered.has("pressure") ? ["pressure"] : []), [settings.pressure, offered]);
  const neighbours = useMemo(
    () => (fieldsAvailable ? [steps[stepIndex + 1], steps[stepIndex - 1]].filter((s): s is string => !!s) : []),
    [fieldsAvailable, steps, stepIndex],
  );
  const layerFields = useFieldGrids(layerVars, debounced.field, neighbours);
  const pressureField = useFieldGrids(pressureVars, debounced.field, neighbours);
  const pressureGrid = settings.pressure ? (pressureField.grids?.pressure ?? null) : null;
  const isobars = useMemo(() => (pressureGrid ? isobarsFor(pressureGrid) : null), [pressureGrid]);
  const scale = useMemo(() => layerScale(layer, index), [layer, index]);
  const particleMode: ParticleMode = !settings.particles || !pageVisible ? "off" : reducedMotion ? "static" : "animated";
  const cities = useCityAirports(zoom >= 4 && settings.basemap === "dark");
  const cityLabels = useMemo(() => {
    const seen = new Set<string>();
    return (cities.data ?? [])
      .filter((a) => {
        const k = `${a.city}|${a.country_code}`;
        if (seen.has(k)) return false;
        seen.add(k);
        return true;
      })
      .map((a) => ({ name: a.city, lat: a.latitude, lon: a.longitude }));
  }, [cities.data]);

  // --- spots ----------------------------------------------------------------------------
  const visible = useMemo(() => spots.filter((s) => passes(s, bySlug.get(s.slug), filters, heightUnits)), [spots, bySlug, filters, heightUnits]);
  const markers = useMemo(() => {
    const out: Record<string, MarkerState> = {};
    for (const s of spots) {
      const c = bySlug.get(s.slug)?.conditions;
      out[s.slug] = c
        ? {
            score: c.score,
            tooltip: `${QUALITY_LABELS[c.label as QualityLabel] ?? c.label} ${c.score}/100 · ${heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, heightUnits)} surf (our prediction)`,
            highlight: !!bySlug.get(s.slug)?.event_at_time,
          }
        : { score: null, tooltip: predictionsCover ? "No usable forecast at this time" : "No surf prediction this far ahead" };
    }
    return out;
  }, [spots, bySlug, heightUnits, predictionsCover]);
  const selectedSpot = spots.find((s) => s.slug === selected);
  const noForecastAnywhere = nowSnapshot.data && !nowSnapshot.data.valid_time;
  const predictionNote = predictionsCover
    ? null
    : range
      ? `Our spot predictions run from ${formatDateTime(range.start, tz)} to ${formatDateTime(range.end, tz)}; the ocean layers continue beyond them.`
      : "No spot predictions for this time.";

  const locate = () => {
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      toast.error("This browser cannot share your location.");
      return;
    }
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setLocating(false);
        setUserLocation({ lat: pos.coords.latitude, lon: pos.coords.longitude, key: Date.now() });
      },
      (err) => {
        setLocating(false);
        toast.error(
          err.code === err.PERMISSION_DENIED
            ? "Location access was denied. Allow it for this site in your browser to jump to where you are."
            : "Your location is not available right now.",
        );
      },
      { enableHighAccuracy: false, timeout: 10_000, maximumAge: 300_000 },
    );
  };

  // Leaflet's own bottom controls (zoom, attribution) sit just above the timeline.
  const bottomRef = useRef<HTMLDivElement>(null);
  const [bottomInset, setBottomInset] = useState(96);
  useEffect(() => {
    const el = bottomRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setBottomInset(Math.round(el.getBoundingClientRect().height)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const listIsOpen = listOpen ?? isDesktop;
  const timeText = at ? bubbleLabel(at, tz) : "";
  const notice = !fieldIndex.isLoading && !fieldsAvailable ? (index?.message ?? "Ocean layers are not available.") : null;

  const spotList = (
    <SpotList
      spots={visible}
      total={spots.length}
      conditions={(slug) => bySlug.get(slug)?.conditions}
      onSelect={setSelected}
      open={listIsOpen}
      onOpenChange={setListOpen}
      updating={snapshot.isFetching}
      note={predictionNote}
      className={isDesktop ? "max-h-full" : "max-h-[45dvh]"}
    />
  );

  return (
    <AppShell fullBleed>
      <h1 className="sr-only">Surf map</h1>
      <div
        className="map-root absolute inset-0 overflow-hidden bg-[#151a21]"
        style={{ isolation: "isolate", "--map-bottom-inset": `${bottomInset}px` } as CSSProperties}
      >
        {!isLoading && (
          <ForecastMap
            spots={visible}
            markers={markers}
            selected={selected}
            onSelect={setSelected}
            basemap={settings.basemap}
            layer={layer}
            fields={fieldsAvailable ? layerFields.grids : null}
            fieldsAt={layerFields.at}
            pressure={pressureGrid}
            isobars={isobars}
            scale={scale}
            particles={particleMode}
            units={units}
            cities={cityLabels.length ? cityLabels : null}
            onZoom={setZoom}
            userLocation={userLocation}
            fieldLabel={fieldsAvailable ? `${def.longName} (${def.label} layer), ${timeText}` : `Ocean layers unavailable, ${timeText}`}
            fieldAttribution={index?.source?.attribution}
          />
        )}

        {/* Top: layer tabs + pressure on the left, settings on the right. */}
        <div className="pointer-events-none absolute inset-x-0 top-0 z-[1100] flex items-start gap-2 p-2 sm:p-3">
          <div className="min-w-0 flex-1 space-y-2">
            <div className="pointer-events-auto w-fit max-w-full">
              <LayerTabs layer={layer} onLayer={(l) => updateSettings({ layer: l })} pressure={settings.pressure} onPressure={(p) => updateSettings({ pressure: p })} />
            </div>
            {(notice || isError || noForecastAnywhere || layerFields.error) && (
              <div className="pointer-events-auto flex w-fit max-w-full flex-col gap-1.5 pl-1">
                {notice && (
                  <p role="status" data-testid="fields-notice" className={cn("flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs", GLASS)}>
                    <Info className="size-3.5 shrink-0 text-[#c4ec4a]" aria-hidden />
                    {notice}
                  </p>
                )}
                {layerFields.error && (
                  <p role="status" className={cn("rounded-full px-3 py-1.5 text-xs", GLASS)}>
                    This ocean layer could not be loaded for the selected time.
                  </p>
                )}
                {isError && <p className="rounded-xl bg-red-950/85 px-3 py-1.5 text-xs text-red-100">Could not load the surf spots. Is the backend running?</p>}
                {noForecastAnywhere && (
                  <p className="max-w-md rounded-xl bg-amber-950/85 px-3 py-1.5 text-xs text-amber-100">
                    No forecast has been ingested yet. Markers are grey until the first forecast run completes (see the status page).
                  </p>
                )}
              </div>
            )}
          </div>
          <div className="pointer-events-auto shrink-0 p-1">
            <SettingsPopover
              settings={settings}
              onSettings={updateSettings}
              heightUnits={heightUnits}
              reducedMotion={reducedMotion}
              spots={spots}
              filters={filters}
              setFilters={setFilters}
              matching={visible.length}
            />
          </div>
        </div>

        {/* Left edge: legend tab and drawer. */}
        <div className="pointer-events-none absolute top-1/2 left-0 z-[1100] -translate-y-1/2">
          <div className="pointer-events-auto">
            <LegendTab open={legendOpen} onToggle={() => setLegendOpen((o) => !o)} />
          </div>
        </div>
        {/* The drawer opens beside the tab, which stays visible to close it again. */}
        {legendOpen && (
          <div className="absolute top-16 left-9 z-[1150]" style={{ bottom: bottomInset + 12 }}>
            <LegendDrawer
              layer={layer}
              scale={scale}
              units={units}
              pressure={settings.pressure}
              index={index}
              particles={particleMode}
              onClose={() => setLegendOpen(false)}
            />
          </div>
        )}

        {/* Right column on large screens: the spot panel, or the best spots. */}
        {isDesktop &&
          (selectedSpot ? (
            <div className="absolute top-16 right-3 z-[1100] flex w-[400px]" style={{ bottom: bottomInset + 26 }}>
              <SpotPanel
                spot={selectedSpot}
                at={bySlug.get(selectedSpot.slug)?.conditions ?? null}
                units={heightUnits}
                signedIn={!!user}
                onClose={() => setSelected(null)}
                className="w-full"
              />
            </div>
          ) : (
            <div
              className="absolute top-16 right-3 z-[1100] flex w-[340px] flex-col"
              // About ten rows: enough to compare the best spots without hiding the map.
              style={{ maxHeight: `min(420px, calc(100% - ${bottomInset + 190}px))` }}
            >
              {spotList}
            </div>
          ))}

        {/* Bottom: locate, (small screens) best spots, and the timeline. */}
        <div ref={bottomRef} className="pointer-events-none absolute inset-x-0 bottom-0 z-[1100] flex flex-col gap-2 p-2 sm:p-3">
          <div className="pointer-events-auto w-fit">
            <button
              type="button"
              onClick={locate}
              aria-label="Show my location"
              className={cn("flex size-11 items-center justify-center rounded-full hover:bg-[#23292f]/90", GLASS, FOCUS)}
            >
              {locating ? <Loader2 className="size-5 animate-spin" aria-hidden /> : <LocateFixed className="size-5" aria-hidden />}
            </button>
          </div>
          {!isDesktop && !selectedSpot && <div className="pointer-events-auto flex flex-col">{spotList}</div>}
          <div className="pointer-events-auto">
            <Timeline
              steps={steps}
              index={stepIndex}
              nowIndex={nowIndex}
              onChange={(i) => setSelectedTime(i === nowIndex ? null : (steps[i] ?? null))}
              playing={playing}
              onPlaying={setPlaying}
              tz={tz}
              busy={layerFields.loading || snapshot.isFetching}
            />
          </div>
        </div>

        {/* Small screens: the spot panel is a bottom sheet over the timeline. */}
        {!isDesktop && selectedSpot && (
          <div className="absolute inset-x-0 bottom-0 z-[1200] flex max-h-[72%] p-1.5">
            <SpotPanel
              spot={selectedSpot}
              at={bySlug.get(selectedSpot.slug)?.conditions ?? null}
              units={heightUnits}
              signedIn={!!user}
              onClose={() => setSelected(null)}
              className="w-full rounded-b-xl"
            />
          </div>
        )}
      </div>
    </AppShell>
  );
}
