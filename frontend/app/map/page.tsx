"use client";

import "./map.css";

import { Info, LocateFixed, Loader2 } from "lucide-react";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { toast } from "sonner";

import { useNowMs } from "@/components/forecast-provenance";
import { ForecastUpdated } from "@/components/forecast-updated";
import { AppShell } from "@/components/layout/app-shell";
import { LayerTabs } from "@/components/map/layer-tabs";
import { LegendDrawer, LegendTab } from "@/components/map/legend-drawer";
import type { ParticleMode } from "@/components/map/map-canvas-layers";
import { MapFreshness, mapStaleness, ModelRunLabel } from "@/components/map/map-freshness";
import { FOCUS, GLASS } from "@/components/map/map-ui";
import { SettingsPopover } from "@/components/map/settings-popover";
import { NO_FILTERS, passes, type Filters } from "@/components/map/spot-filters";
import { SpotList } from "@/components/map/spot-list";
import { SpotPanel } from "@/components/map/spot-panel";
import type { MarkerState } from "@/components/map/surf-map";
import { Timeline } from "@/components/map/timeline";
import { useCityAirports, useFieldIndex, useMapLayerFields } from "@/hooks/map-fields";
import { useMediaQuery, usePageVisible, usePrefersReducedMotion } from "@/hooks/map-media";
import { useMapSettings } from "@/hooks/map-settings";
import { useMapConditions, useMe, useSpots } from "@/hooks/queries";
import { freshnessOf } from "@/lib/forecast-fields";
import { compass, formatDateTime, heightRange, updatedAgo, type Units } from "@/lib/format";
import { isobarsFor } from "@/lib/map-geo";
import { layerDef, layerScale, type DisplayUnits } from "@/lib/map-layers";
import { bubbleLabel, longTimeLabel, nearestStepIndex, timelineSteps } from "@/lib/map-time";
import { QUALITY_LABELS, WIND_RELATION_LABEL } from "@/lib/quality";
import { score10 } from "@/lib/rating";
import { formatWind, useHeightUnit } from "@/lib/units";
import { cn } from "@/lib/utils";
import type { QualityLabel } from "@/types/api";

const ForecastMap = dynamic(() => import("@/components/map/forecast-map"), {
  ssr: false,
  loading: () => <div className="absolute inset-0 bg-[#151a21]" />,
});

/** A spot prediction is shown for a step only if the server found one within this window. */
const PREDICTION_WINDOW_MS = 90 * 60_000;
/** City labels (and their airports request) from this zoom in. */
const CITY_ZOOM = 4;
/** Phones: the spot panel is a bottom sheet up to this fraction of the map's height. */
const SHEET_FRACTION = 0.72;
/** Short, wide screens (phones in landscape): the spot panel is a side sheet this wide (px). */
const SIDE_SHEET_WIDTH = 360;
/** Phones: CSS pixels under the layer tabs at the top of the map, until the top controls (the
 * tabs, the "Forecast updated" pill and any notices) are measured. */
const TABS_INSET = 64;
/** Room left between the top controls and a selected spot (CSS pixels). */
const TOP_MARGIN = 8;

export default function MapPage() {
  // A failed background re-read (a new hour, a refocus) keeps the spots on screen: the
  // notice is for a first load that failed.
  const { data: spots = [], isLoading, isLoadingError: isError } = useSpots();
  const { data: user } = useMe();
  const [settings, updateMapSettings] = useMapSettings();
  // One height unit for the whole site (the spot charts too): this browser's choice, else the
  // map's older own setting, else the account's units, else feet.
  const [heightUnits, setHeightUnits] = useHeightUnit(settings.heightUnits ?? (user?.profile.units as Units | undefined) ?? "ft");
  const updateSettings = useCallback(
    (patch: Partial<typeof settings>) => {
      if (patch.heightUnits) setHeightUnits(patch.heightUnits);
      updateMapSettings(patch);
    },
    [setHeightUnits, updateMapSettings],
  );
  const units: DisplayUnits = useMemo(() => ({ height: heightUnits, wind: settings.windUnits }), [heightUnits, settings.windUnits]);
  const tz = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const reducedMotion = usePrefersReducedMotion();
  const pageVisible = usePageVisible();
  const isDesktop = useMediaQuery("(min-width: 1024px)");
  // Phones in landscape: a bottom sheet left a strip of about 30 px of map above it, so the
  // spot panel opens beside the map (and the timeline) instead.
  const sideSheet = useMediaQuery("(min-width: 640px) and (max-height: 500px)") && !isDesktop;

  const [selected, setSelected] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [legendOpen, setLegendOpen] = useState(false);
  const [listOpen, setListOpen] = useState<boolean | null>(null);
  const [playing, setPlaying] = useState(false);
  // Only crossing CITY_ZOOM matters to the page: other zoom changes do not re-render it.
  const [cityZoom, setCityZoom] = useState(false);
  const onZoom = useCallback((z: number) => setCityZoom(z >= CITY_ZOOM), []);
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

  // Playback holds while the tab is hidden (no downloads nobody sees) and resumes when shown.
  useEffect(() => {
    if (!playing || !pageVisible) return;
    const t = setTimeout(() => {
      if (stepIndex >= steps.length - 1) setPlaying(false);
      else setSelectedTime(steps[stepIndex + 1] ?? null);
    }, 1100);
    return () => clearTimeout(t);
  }, [playing, pageVisible, stepIndex, steps]);

  // --- ocean layers ---------------------------------------------------------------------
  const layer = settings.layer;
  const def = layerDef(layer);
  // The settled (debounced) time: fields and their prefetch follow it, not every step scrubbed.
  const layerFields = useMapLayerFields({ settings, at: debounced.field, index: index ?? null });
  const pressureGrid = layerFields.pressure;
  const isobars = useMemo(() => (pressureGrid ? isobarsFor(pressureGrid) : null), [pressureGrid]);
  const scale = useMemo(() => layerScale(layer, index), [layer, index]);
  const particleMode: ParticleMode = !settings.particles || !pageVisible ? "off" : reducedMotion ? "static" : "animated";
  const cities = useCityAirports(cityZoom && settings.basemap === "dark");
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
  const now = useNowMs();
  const markers = useMemo(() => {
    const out: Record<string, MarkerState> = {};
    for (const s of spots) {
      const m = bySlug.get(s.slug);
      const c = m?.conditions;
      // A spot whose served forecast is out of date says so, and when it was last updated.
      const fresh = freshnessOf(m);
      const staleText = fresh?.stale ? (fresh.lastUpdatedAt ? `Forecast last updated ${updatedAgo(fresh.lastUpdatedAt, now)}` : "Forecast out of date") : null;
      if (!c) {
        out[s.slug] = { score: null, tooltip: predictionsCover ? "No usable forecast at this time" : "No surf prediction this far ahead", detail: staleText, stale: !!fresh?.stale };
        continue;
      }
      const surf = heightRange(c.breaking_height_min_ft, c.breaking_height_max_ft, heightUnits);
      const swell = c.swell_direction_deg != null ? `Swell from ${compass(c.swell_direction_deg)}` : null;
      const wind =
        c.wind_speed_kmh != null
          ? `wind ${formatWind(c.wind_speed_kmh, settings.windUnits)}${c.wind_direction_deg != null ? ` from ${compass(c.wind_direction_deg)}` : ""}${c.wind_relation && c.wind_relation !== "unknown" ? `, ${(WIND_RELATION_LABEL[c.wind_relation] ?? c.wind_relation).toLowerCase()}` : ""}`
          : null;
      const detail = [swell, wind].filter(Boolean).join(" · ");
      out[s.slug] = {
        score: c.score,
        tooltip: `${QUALITY_LABELS[c.label as QualityLabel] ?? c.label} ${score10(c.score)}/10 · ${surf} surf (our prediction)`,
        detail: [detail ? detail.charAt(0).toUpperCase() + detail.slice(1) : null, staleText].filter(Boolean).join(" · ") || null,
        highlight: !!m?.event_at_time,
        swellFromDeg: c.swell_direction_deg,
        windFromDeg: c.wind_direction_deg,
        label: surf,
        stale: !!fresh?.stale,
      };
    }
    return out;
  }, [spots, bySlug, heightUnits, predictionsCover, settings.windUnits, now]);
  const selectedSpot = spots.find((s) => s.slug === selected);
  const noForecastAnywhere = nowSnapshot.data && !nowSnapshot.data.valid_time;
  const predictionNote = predictionsCover
    ? null
    : range
      ? `No surf predictions for this time: ours run from ${formatDateTime(range.start, tz)} to ${formatDateTime(range.end, tz)}. The ocean layers cover a longer period.`
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
  const timelineRef = useRef<HTMLDivElement>(null);
  const [bottomInset, setBottomInset] = useState(96);
  // The timeline alone (without the locate row above it), from the bottom of the map.
  const [timelineInset, setTimelineInset] = useState(76);
  // The top controls' bottom edge in the map (the tabs, the pill drawn under them once a
  // version is known, and the notices): a selected spot lands below it (review r1-frontend-FE-3).
  const topRef = useRef<HTMLDivElement>(null);
  const [topInset, setTopInset] = useState(TABS_INSET);
  useEffect(() => {
    const el = topRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const root = el.parentElement?.getBoundingClientRect();
      const box = el.getBoundingClientRect();
      if (root) setTopInset(Math.max(TABS_INSET, Math.round(box.bottom - root.top) + TOP_MARGIN));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    const el = bottomRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const box = el.getBoundingClientRect();
      setBottomInset(Math.round(box.height));
      const timeline = timelineRef.current?.getBoundingClientRect();
      if (timeline) setTimelineInset(Math.round(box.bottom - timeline.top));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const listIsOpen = listOpen ?? isDesktop;
  const timeText = at ? bubbleLabel(at, tz) : "";
  // A selected spot lands below the layer tabs and clear of the open panel: above the phone's
  // bottom sheet, or left of the side sheet and above the timeline (the locate button at the
  // far left is clear of the middle, where the spot lands).
  const cover = useCallback(
    (size: { y: number }) =>
      sideSheet
        ? { top: topInset, right: SIDE_SHEET_WIDTH, bottom: timelineInset, left: 0 }
        : { top: topInset, right: 0, bottom: size.y * SHEET_FRACTION, left: 0 },
    [sideSheet, timelineInset, topInset],
  );
  // The canvas keeps the previous step's field until the next one arrives: describe what it shows.
  const fieldTime = layerFields.servedValidTime ?? at;
  const fieldTimeText = fieldTime ? longTimeLabel(fieldTime, tz) : "";
  // One short line for everyone; the backend's setup message (for whoever runs the site) is in
  // the legend. Its full text covered a block of the map on phones and ran under the spot list.
  const notice = !fieldIndex.isLoading && !fieldsAvailable ? "Ocean layers are unavailable right now." : null;
  const stale = mapStaleness(index, nowSnapshot.data, nowSnapshot.dataUpdatedAt).stale;

  const spotList = (
    <SpotList
      spots={visible}
      total={spots.length}
      conditions={(slug) => bySlug.get(slug)?.conditions}
      freshness={(slug) => freshnessOf(bySlug.get(slug))}
      onSelect={setSelected}
      open={listIsOpen}
      onOpenChange={setListOpen}
      updating={snapshot.isFetching}
      note={predictionNote}
      timeLabel={at && stepIndex !== nowIndex ? timeText : null}
      links={(snapshot.data ?? nowSnapshot.data)?.attribution_links}
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
        {/* Top: layer tabs + pressure on the left, settings on the right. */}
        <div className="pointer-events-none absolute inset-x-0 top-0 z-[1100] flex items-start gap-2 p-2 sm:p-3">
          <div ref={topRef} className="min-w-0 flex-1 space-y-2" data-testid="map-top-controls">
            <div className="pointer-events-auto w-fit max-w-full">
              <LayerTabs layer={layer} onLayer={(l) => updateSettings({ layer: l })} pressure={settings.pressure} onPressure={(p) => updateSettings({ pressure: p })} />
            </div>
            {/* Beside a selected out-of-date spot "updated just now" would call it current. */}
            <ForecastUpdated tone="map" stale={!!selected && !!freshnessOf(bySlug.get(selected))?.stale} className="pointer-events-auto ml-1" />
            {(notice || stale || isError || noForecastAnywhere || layerFields.error) && (
              <div className="pointer-events-auto flex w-fit max-w-full flex-col gap-1.5 pl-1">
                {/* The newest model run is out of date: the layers and markers still show it. */}
                <MapFreshness index={index} snapshot={nowSnapshot.data} now={nowSnapshot.dataUpdatedAt} />
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
              onChange={updateSettings}
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
              settings={settings}
              scale={scale}
              units={units}
              index={index}
              particles={particleMode}
              onClose={() => setLegendOpen(false)}
            />
          </div>
        )}

        {/* Right column on large screens: the spot panel, or the best spots. */}
        {isDesktop &&
          (selectedSpot ? (
            // Ends above Leaflet's zoom buttons and attribution (about 100 px over the timeline),
            // so both stay usable while a spot is open.
            <div className="absolute top-16 right-3 z-[1100] flex w-[400px]" style={{ bottom: bottomInset + 104 }}>
              <SpotPanel
                spot={selectedSpot}
                at={bySlug.get(selectedSpot.slug)?.conditions ?? null}
                time={at}
                linkTime={selectedTime ? at : null}
                units={heightUnits}
                signedIn={!!user}
                onClose={() => setSelected(null)}
                links={(snapshot.data ?? nowSnapshot.data)?.attribution_links}
                freshness={freshnessOf(bySlug.get(selectedSpot.slug))}
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

        {/* Bottom: locate, (small screens) best spots, and the timeline with the model run
            beside its bubble. From 640 px wide (landscape phones, tablets) the best spots share
            the first row, so a short map keeps room for its own controls. */}
        <div
          ref={bottomRef}
          className="pointer-events-none absolute inset-x-0 bottom-0 z-[1100] flex flex-col gap-2 p-2 sm:p-3"
          // Beside an open side sheet, so the whole timeline stays usable.
          style={sideSheet && selectedSpot ? { right: SIDE_SHEET_WIDTH } : undefined}
        >
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
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
            {!isDesktop && !selectedSpot && <div className="pointer-events-auto flex flex-col sm:ml-auto sm:w-[340px] sm:shrink-0">{spotList}</div>}
          </div>
          <div ref={timelineRef} className="pointer-events-auto">
            <Timeline
              steps={steps}
              index={stepIndex}
              nowIndex={nowIndex}
              onChange={(i) => setSelectedTime(i === nowIndex ? null : (steps[i] ?? null))}
              playing={playing}
              onPlaying={setPlaying}
              tz={tz}
              busy={layerFields.loading || snapshot.isFetching}
              aside={<ModelRunLabel index={index} at={at} />}
            />
          </div>
        </div>

        {/* Small screens: the spot panel is a bottom sheet over the timeline; short, wide ones
            (phones in landscape) get a side sheet below the settings button instead. */}
        {!isDesktop && selectedSpot && (
          <div
            className={cn("absolute z-[1200] flex p-1.5", sideSheet ? "top-16 right-0 bottom-0" : "inset-x-0 bottom-0")}
            style={sideSheet ? { width: SIDE_SHEET_WIDTH } : { maxHeight: `${SHEET_FRACTION * 100}%` }}
          >
            <SpotPanel
              spot={selectedSpot}
              at={bySlug.get(selectedSpot.slug)?.conditions ?? null}
              time={at}
              linkTime={selectedTime ? at : null}
              units={heightUnits}
              signedIn={!!user}
              onClose={() => setSelected(null)}
              links={(snapshot.data ?? nowSnapshot.data)?.attribution_links}
              freshness={freshnessOf(bySlug.get(selectedSpot.slug))}
              className="w-full rounded-b-xl"
            />
          </div>
        )}

        {/* The map comes last in the DOM, so keyboard users reach the controls above (layers,
            settings, legend, best spots, timeline) before its 50 markers. The controls stay on
            top by their z-index. */}
        {!isLoading && (
          <ForecastMap
            spots={visible}
            markers={markers}
            selected={selected}
            onSelect={setSelected}
            settings={settings}
            fields={layerFields.fields}
            fieldsAt={layerFields.servedValidTime}
            pressure={pressureGrid}
            isobars={isobars}
            scale={scale}
            particles={particleMode}
            units={units}
            cities={cityLabels.length ? cityLabels : null}
            onZoom={onZoom}
            userLocation={userLocation}
            cover={isDesktop ? undefined : cover}
            fieldLabel={fieldsAvailable ? `${def.longName} (${def.label} layer), ${fieldTimeText}` : `Ocean layers unavailable, ${timeText}`}
            // The map's credit strip names the source; its full attribution sentence is in the
            // legend (on a phone the sentence alone covered three lines of the map, and markers).
            fieldAttribution={index?.source?.name ?? index?.source?.attribution}
          />
        )}
      </div>
    </AppShell>
  );
}
