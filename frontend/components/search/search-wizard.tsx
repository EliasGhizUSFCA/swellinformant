"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, ChevronDown, Info } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { AirportPicker } from "@/components/airport-picker";
import { Field } from "@/components/field";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioCard, RadioGroup } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { qk, useRegions, useSpots } from "@/hooks/queries";
import { ApiError, errorMessage, post, put } from "@/lib/api";
import { fromUnits, toUnits } from "@/lib/format";
import { QUALITY_COLORS, QUALITY_OPTIONS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import { toSearchInput, validateStep, WIZARD_STEPS, type StepKey, type WizardState } from "@/lib/validation";
import type { Search, User } from "@/types/api";

const BREAK_TYPES = ["reef", "point", "beach", "rivermouth", "slab"];
const CURRENCIES = ["USD", "EUR", "GBP", "AUD", "CAD", "NZD", "ZAR", "BRL", "JPY"];

function numberOrNull(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function SearchWizard({ user, initial, searchId }: { user: User; initial: WizardState; searchId?: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [state, setState] = useState<WizardState>(initial);
  const [stepIndex, setStepIndex] = useState(0);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const step = WIZARD_STEPS[stepIndex]!.key;

  const set = <K extends keyof WizardState>(key: K, value: WizardState[K]) => setState((s) => ({ ...s, [key]: value }));

  function go(to: number) {
    if (to > stepIndex) {
      const errs = validateStep(step, state);
      setErrors(errs);
      if (Object.keys(errs).length) return;
    }
    setErrors({});
    setStepIndex(to);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function submit() {
    const errs = validateStep("review", state);
    setErrors(errs);
    if (Object.keys(errs).length) {
      setServerError("Please fix the highlighted fields.");
      return;
    }
    setSubmitting(true);
    setServerError(null);
    try {
      const body = toSearchInput(state);
      const saved = searchId ? await put<Search>(`/api/searches/${searchId}`, body) : await post<Search>("/api/searches", body);
      await queryClient.invalidateQueries({ queryKey: qk.searches });
      toast.success(searchId ? "Search updated." : "Search activated — we're watching the swell for you.");
      router.push(`/searches/${saved.id}`);
    } catch (e) {
      setServerError(errorMessage(e));
      if (e instanceof ApiError) setErrors(e.fieldErrors());
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="grid gap-8 lg:grid-cols-[240px_1fr]">
      <ol className="flex gap-2 overflow-x-auto lg:flex-col lg:overflow-visible" aria-label="Steps">
        {WIZARD_STEPS.map((s, i) => (
          <li key={s.key}>
            <button
              type="button"
              onClick={() => (i < stepIndex ? go(i) : undefined)}
              className={cn(
                "flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm whitespace-nowrap transition",
                i === stepIndex ? "bg-card font-semibold text-ink shadow-sm" : "text-muted-foreground",
                i < stepIndex && "cursor-pointer hover:bg-card/60",
              )}
              aria-current={i === stepIndex ? "step" : undefined}
            >
              <span
                className={cn(
                  "flex size-7 shrink-0 items-center justify-center rounded-full border text-xs",
                  i < stepIndex && "border-primary bg-primary text-white",
                  i === stepIndex && "border-primary text-primary",
                )}
              >
                {i < stepIndex ? <Check className="size-3.5" /> : i + 1}
              </span>
              {s.title}
            </button>
          </li>
        ))}
      </ol>

      <Card>
        <CardContent className="p-6 sm:p-8">
          <h2 className="mb-1 font-display text-2xl font-semibold text-ink">{WIZARD_STEPS[stepIndex]!.title}</h2>
          <p className="mb-6 text-sm text-muted-foreground">Step {stepIndex + 1} of {WIZARD_STEPS.length}</p>

          {step === "surf" && <SurfStep state={state} set={set} errors={errors} />}
          {step === "destinations" && <DestinationsStep state={state} set={set} errors={errors} />}
          {step === "dates" && <DatesStep state={state} set={set} errors={errors} />}
          {step === "flights" && <FlightsStep state={state} set={set} errors={errors} user={user} />}
          {step === "notifications" && <NotificationsStep state={state} set={set} user={user} />}
          {step === "review" && <ReviewStep state={state} set={set} errors={errors} goTo={go} />}

          {serverError && (
            <Alert variant="danger" className="mt-6">
              <AlertDescription>{serverError}</AlertDescription>
            </Alert>
          )}

          <div className="mt-8 flex items-center justify-between border-t pt-6">
            <Button variant="ghost" onClick={() => go(stepIndex - 1)} disabled={stepIndex === 0} type="button">
              <ArrowLeft /> Back
            </Button>
            {step === "review" ? (
              <Button onClick={submit} disabled={submitting} size="lg" type="button">
                {submitting ? "Saving…" : searchId ? "Save changes" : "Activate search"}
              </Button>
            ) : (
              <Button onClick={() => go(stepIndex + 1)} type="button">
                Continue <ArrowRight />
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

type StepProps = {
  state: WizardState;
  set: <K extends keyof WizardState>(key: K, value: WizardState[K]) => void;
  errors?: Record<string, string>;
};

function NumberInput({
  id,
  value,
  onChange,
  step = 1,
  min,
  max,
  suffix,
  invalid,
}: {
  id: string;
  value: number | null;
  onChange: (v: number | null) => void;
  step?: number;
  min?: number;
  max?: number;
  suffix?: string;
  invalid?: boolean;
}) {
  return (
    <div className="relative">
      <Input
        id={id}
        type="number"
        inputMode="decimal"
        step={step}
        min={min}
        max={max}
        value={value ?? ""}
        onChange={(e) => onChange(numberOrNull(e.target.value))}
        aria-invalid={invalid}
        className={suffix ? "pr-14" : undefined}
      />
      {suffix && <span className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-xs text-muted-foreground">{suffix}</span>}
    </div>
  );
}

function SurfStep({ state, set, errors = {} }: StepProps) {
  const [advanced, setAdvanced] = useState(
    state.min_period_s != null || state.wind_requirement !== "any" || state.break_types.length > 0,
  );
  const u = state.units;

  function switchUnits(next: "ft" | "m") {
    if (next === u) return;
    const conv = (v: number) => Math.round(toUnits(fromUnits(v, u), next) * 10) / 10;
    set("units", next);
    set("wave_min", conv(state.wave_min));
    set("wave_max", conv(state.wave_max));
    set("wave_preferred", state.wave_preferred == null ? null : conv(state.wave_preferred));
  }

  return (
    <div className="space-y-7">
      <div>
        <div className="mb-3 flex items-center justify-between">
          <Label>Desired wave height</Label>
          <div className="inline-flex rounded-lg bg-muted p-0.5 text-xs" role="group" aria-label="Height units">
            {(["ft", "m"] as const).map((x) => (
              <button
                key={x}
                type="button"
                onClick={() => switchUnits(x)}
                className={cn("rounded-md px-3 py-1 font-medium", u === x ? "bg-card shadow-sm" : "text-muted-foreground")}
                aria-pressed={u === x}
              >
                {x === "ft" ? "Feet" : "Metres"}
              </button>
            ))}
          </div>
        </div>
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Minimum" htmlFor="wave_min" error={errors.wave_min}>
            <NumberInput id="wave_min" value={state.wave_min} onChange={(v) => set("wave_min", v ?? 0)} step={0.5} min={0} suffix={u} invalid={!!errors.wave_min} />
          </Field>
          <Field label="Maximum" htmlFor="wave_max" error={errors.wave_max}>
            <NumberInput id="wave_max" value={state.wave_max} onChange={(v) => set("wave_max", v ?? 0)} step={0.5} min={0} suffix={u} invalid={!!errors.wave_max} />
          </Field>
          <Field label="Preferred (optional)" htmlFor="wave_preferred" error={errors.wave_preferred}>
            <NumberInput id="wave_preferred" value={state.wave_preferred} onChange={(v) => set("wave_preferred", v)} step={0.5} min={0} suffix={u} />
          </Field>
        </div>
        <p className="mt-2 flex gap-2 text-xs text-muted-foreground">
          <Info className="mt-0.5 size-3.5 shrink-0" />
          <span>
            These are estimated <strong className="font-medium">breaking face heights</strong> at the break — not the offshore
            significant wave height that models report. A 2 m offshore swell can mean anything from 3 ft to 12 ft depending on
            period, direction and the spot.
          </span>
        </p>
      </div>

      <div>
        <Label className="mb-3">Minimum surf quality</Label>
        <RadioGroup
          value={state.min_quality}
          onValueChange={(v) => set("min_quality", v as WizardState["min_quality"])}
          className="grid gap-2 sm:grid-cols-5"
          aria-label="Minimum surf quality"
        >
          {QUALITY_OPTIONS.map((q) => (
            <RadioCard key={q.value} value={q.value} className="p-3">
              <span className="flex items-center gap-2 text-sm font-semibold">
                <span className="size-2.5 rounded-full" style={{ backgroundColor: QUALITY_COLORS[q.value] }} />
                {q.label}
              </span>
              <span className="text-[11px] text-muted-foreground">Score ≥ {q.min}</span>
            </RadioCard>
          ))}
        </RadioGroup>
        <p className="mt-2 text-xs text-muted-foreground">
          {QUALITY_OPTIONS.find((q) => q.value === state.min_quality)?.hint} Scores combine swell direction, period, size, wind,
          tide and consistency with documented weights.
        </p>
      </div>

      <div className="rounded-xl border">
        <button
          type="button"
          onClick={() => setAdvanced((v) => !v)}
          className="flex w-full items-center justify-between px-4 py-3 text-sm font-medium"
          aria-expanded={advanced}
        >
          Wave characteristics (optional)
          <ChevronDown className={cn("size-4 transition", advanced && "rotate-180")} />
        </button>
        {advanced && (
          <div className="grid gap-4 border-t p-4 sm:grid-cols-2">
            <Field label="Minimum swell period" htmlFor="min_period_s" hint="Longer period = more powerful groundswell.">
              <NumberInput id="min_period_s" value={state.min_period_s} onChange={(v) => set("min_period_s", v)} min={4} max={25} suffix="s" />
            </Field>
            <Field label="Wind" htmlFor="wind_requirement">
              <Select value={state.wind_requirement} onValueChange={(v) => set("wind_requirement", v as WizardState["wind_requirement"])}>
                <SelectTrigger id="wind_requirement">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="any">Any wind (score decides)</SelectItem>
                  <SelectItem value="not_onshore">No onshore wind</SelectItem>
                  <SelectItem value="offshore">Offshore or glassy only</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field label="Maximum wind speed" htmlFor="max_wind_kmh">
              <NumberInput id="max_wind_kmh" value={state.max_wind_kmh} onChange={(v) => set("max_wind_kmh", v)} min={0} max={100} suffix="km/h" />
            </Field>
            <Field label="Minimum surf window" htmlFor="min_window_hours" hint="Daylight hours that must meet all your criteria." error={errors.min_window_hours}>
              <NumberInput id="min_window_hours" value={state.min_window_hours} onChange={(v) => set("min_window_hours", v ?? 1)} min={1} max={240} suffix="hours" />
            </Field>
            <Field label="Swell direction from" htmlFor="swell_direction_min" hint="Compass degrees, clockwise window.">
              <NumberInput id="swell_direction_min" value={state.swell_direction_min} onChange={(v) => set("swell_direction_min", v)} min={0} max={359} suffix="°" />
            </Field>
            <Field label="Swell direction to" htmlFor="swell_direction_max" error={errors.swell_direction_max}>
              <NumberInput id="swell_direction_max" value={state.swell_direction_max} onChange={(v) => set("swell_direction_max", v)} min={0} max={359} suffix="°" />
            </Field>
            <Field label="Minimum consistency" htmlFor="min_consistency" hint="Share of wave energy in organised swell vs. local wind chop.">
              <NumberInput id="min_consistency" value={state.min_consistency} onChange={(v) => set("min_consistency", v)} min={0} max={100} suffix="%" />
            </Field>
            <div className="space-y-2">
              <Label>Break types</Label>
              <div className="flex flex-wrap gap-2">
                {BREAK_TYPES.map((b) => {
                  const on = state.break_types.includes(b);
                  return (
                    <button
                      key={b}
                      type="button"
                      onClick={() => set("break_types", on ? state.break_types.filter((x) => x !== b) : [...state.break_types, b])}
                      className={cn(
                        "rounded-full border px-3 py-1 text-sm capitalize transition",
                        on ? "border-primary bg-primary text-white" : "hover:border-primary/50",
                      )}
                      aria-pressed={on}
                    >
                      {b}
                    </button>
                  );
                })}
              </div>
              <p className="text-xs text-muted-foreground">None selected = all break types.</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function DestinationsStep({ state, set, errors = {} }: StepProps) {
  const { data: regions = [] } = useRegions();
  const { data: spots = [] } = useSpots();
  const [filter, setFilter] = useState("");
  const grouped = useMemo(() => {
    const f = filter.trim().toLowerCase();
    const map = new Map<string, typeof spots>();
    for (const s of spots) {
      if (f && !`${s.name} ${s.country} ${s.region}`.toLowerCase().includes(f)) continue;
      map.set(s.region_group, [...(map.get(s.region_group) ?? []), s]);
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [spots, filter]);

  const toggle = (key: "spot_slugs" | "region_groups" | "countries", value: string) => {
    const list = state[key];
    set(key, list.includes(value) ? list.filter((x) => x !== value) : [...list, value]);
  };

  return (
    <div className="space-y-6">
      <RadioGroup
        value={state.destination_mode}
        onValueChange={(v) => set("destination_mode", v as WizardState["destination_mode"])}
        className="grid gap-3 sm:grid-cols-3"
        aria-label="Destination mode"
      >
        <RadioCard value="all">
          <span className="font-semibold">All 50 spots</span>
          <span className="text-xs text-muted-foreground">Let the swell pick anywhere in the world.</span>
        </RadioCard>
        <RadioCard value="regions">
          <span className="font-semibold">Regions or countries</span>
          <span className="text-xs text-muted-foreground">e.g. Indonesia, or just Portugal.</span>
        </RadioCard>
        <RadioCard value="spots">
          <span className="font-semibold">Specific spots</span>
          <span className="text-xs text-muted-foreground">Your personal bucket list.</span>
        </RadioCard>
      </RadioGroup>

      {state.destination_mode === "regions" && (
        <div className="space-y-4">
          {errors.region_groups && <p className="text-sm text-destructive">{errors.region_groups}</p>}
          {regions.map((r) => (
            <div key={r.region_group} className="rounded-xl border p-4">
              <label className="flex items-center gap-3 font-medium">
                <Checkbox checked={state.region_groups.includes(r.region_group)} onCheckedChange={() => toggle("region_groups", r.region_group)} />
                {r.region_group}
                <span className="text-xs font-normal text-muted-foreground">{r.spot_count} spots</span>
              </label>
              {!state.region_groups.includes(r.region_group) && (
                <div className="mt-3 flex flex-wrap gap-2 pl-7">
                  {r.countries.map((c) => {
                    const code = c.code ?? "";
                    const on = state.countries.includes(code);
                    return (
                      <button
                        key={code}
                        type="button"
                        onClick={() => toggle("countries", code)}
                        className={cn("rounded-full border px-3 py-1 text-xs transition", on ? "border-primary bg-primary text-white" : "hover:border-primary/50")}
                        aria-pressed={on}
                      >
                        {c.name}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {state.destination_mode === "spots" && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Input placeholder="Filter spots…" value={filter} onChange={(e) => setFilter(e.target.value)} className="max-w-xs" aria-label="Filter spots" />
            <span className="text-sm text-muted-foreground">{state.spot_slugs.length} selected</span>
          </div>
          {errors.spot_slugs && <p className="text-sm text-destructive">{errors.spot_slugs}</p>}
          <div className="max-h-[420px] space-y-4 overflow-auto rounded-xl border p-4 scrollbar-thin">
            {grouped.map(([group, list]) => (
              <div key={group}>
                <div className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">{group}</div>
                <div className="grid gap-1 sm:grid-cols-2">
                  {list.map((s) => (
                    <label key={s.slug} className="flex cursor-pointer items-center gap-3 rounded-lg px-2 py-1.5 text-sm hover:bg-muted">
                      <Checkbox checked={state.spot_slugs.includes(s.slug)} onCheckedChange={() => toggle("spot_slugs", s.slug)} aria-label={s.name} />
                      <span className="min-w-0 truncate">
                        {s.name} <span className="text-muted-foreground">· {s.country}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <Field label="Maximum airport-to-spot transfer" htmlFor="max_transfer" hint="Spots whose nearest practical airport is further away become surf-only watches.">
        <Select
          value={state.max_transfer_minutes == null ? "any" : String(state.max_transfer_minutes)}
          onValueChange={(v) => set("max_transfer_minutes", v === "any" ? null : Number(v))}
        >
          <SelectTrigger id="max_transfer" className="max-w-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="any">No limit</SelectItem>
            <SelectItem value="60">Up to 1 hour</SelectItem>
            <SelectItem value="120">Up to 2 hours</SelectItem>
            <SelectItem value="240">Up to 4 hours</SelectItem>
            <SelectItem value="480">Up to 8 hours (boat trips)</SelectItem>
          </SelectContent>
        </Select>
      </Field>
    </div>
  );
}

function DatesStep({ state, set, errors = {} }: StepProps) {
  return (
    <div className="space-y-6">
      <RadioGroup
        value={state.date_mode}
        onValueChange={(v) => set("date_mode", v as WizardState["date_mode"])}
        className="grid gap-3 sm:grid-cols-2"
        aria-label="Date mode"
      >
        <RadioCard value="flexible">
          <span className="font-semibold">Flexible — keep watching</span>
          <span className="text-xs text-muted-foreground">Alert me whenever a swell lines up within my horizon.</span>
        </RadioCard>
        <RadioCard value="fixed">
          <span className="font-semibold">Fixed dates</span>
          <span className="text-xs text-muted-foreground">Only swells that fit inside my vacation window.</span>
        </RadioCard>
      </RadioGroup>

      {state.date_mode === "flexible" ? (
        <Field label="Monitoring horizon" htmlFor="horizon_days" error={errors.horizon_days} hint="Alerts target swells ~5–10 days out; the horizon caps how far ahead you'd travel.">
          <NumberInput id="horizon_days" value={state.horizon_days} onChange={(v) => set("horizon_days", v ?? 30)} min={1} max={365} suffix="days" />
        </Field>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Earliest day I can leave home" htmlFor="date_start" error={errors.date_start}>
            <Input id="date_start" type="date" value={state.date_start} onChange={(e) => set("date_start", e.target.value)} />
          </Field>
          <Field label="Latest day I must be back" htmlFor="date_end" error={errors.date_end}>
            <Input id="date_end" type="date" value={state.date_end} onChange={(e) => set("date_end", e.target.value)} />
          </Field>
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Arrive before the swell" htmlFor="arrival_buffer_days" hint="Days early, in the destination's local calendar.">
          <NumberInput id="arrival_buffer_days" value={state.arrival_buffer_days} onChange={(v) => set("arrival_buffer_days", v ?? 0)} min={0} max={14} suffix="days" />
        </Field>
        <Field label="Leave after the swell" htmlFor="departure_buffer_days">
          <NumberInput id="departure_buffer_days" value={state.departure_buffer_days} onChange={(v) => set("departure_buffer_days", v ?? 0)} min={0} max={14} suffix="days" />
        </Field>
        <Field label="Minimum days at destination" htmlFor="min_days">
          <NumberInput id="min_days" value={state.min_days_at_destination} onChange={(v) => set("min_days_at_destination", v ?? 1)} min={1} max={60} suffix="days" />
        </Field>
        <Field label="Maximum trip length" htmlFor="max_trip_days" error={errors.max_trip_days}>
          <NumberInput id="max_trip_days" value={state.max_trip_days} onChange={(v) => set("max_trip_days", v ?? 1)} min={1} max={90} suffix="days" />
        </Field>
      </div>
      <Alert variant="info">
        <Info />
        <AlertDescription>
          Example: a swell on Aug 10–13 with “arrive {state.arrival_buffer_days} days early” and “leave {state.departure_buffer_days}{" "}
          day after” means landing by Aug {10 - state.arrival_buffer_days} and flying home on Aug {13 + state.departure_buffer_days}. We
          check each itinerary&apos;s real arrival time, the time-zone change and the airport transfer.
        </AlertDescription>
      </Alert>
    </div>
  );
}

function FlightsStep({ state, set, errors = {}, user }: StepProps & { user: User }) {
  const saved = user.airports.map((a) => a.airport_iata).filter((c) => !state.origins.includes(c));
  return (
    <div className="space-y-6">
      <Field label="Departure airports" htmlFor="origins" error={errors.origins} hint="Up to 5. We search each and pick the best itinerary.">
        <AirportPicker id="origins" value={state.origins} onChange={(v) => set("origins", v)} />
      </Field>
      {saved.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">Saved:</span>
          {saved.map((c) => (
            <button key={c} type="button" className="rounded-full border px-2.5 py-0.5 hover:border-primary" onClick={() => set("origins", [...state.origins, c].slice(0, 5))}>
              + {c}
            </button>
          ))}
        </div>
      )}
      <Field label="Also consider airports within" htmlFor="nearby" hint="Ground travel you'd accept to reach another departure airport.">
        <Select value={String(state.max_origin_ground_km)} onValueChange={(v) => set("max_origin_ground_km", Number(v))}>
          <SelectTrigger id="nearby" className="max-w-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="0">Only the airports above</SelectItem>
            <SelectItem value="100">100 km</SelectItem>
            <SelectItem value="200">200 km</SelectItem>
            <SelectItem value="350">350 km</SelectItem>
          </SelectContent>
        </Select>
      </Field>

      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Max round-trip fare per traveller" htmlFor="max_price" error={errors.max_price} className="sm:col-span-2">
          <NumberInput id="max_price" value={state.max_price} onChange={(v) => set("max_price", v ?? 0)} min={1} step={10} invalid={!!errors.max_price} />
        </Field>
        <Field label="Currency" htmlFor="currency">
          <Select value={state.currency} onValueChange={(v) => set("currency", v)}>
            <SelectTrigger id="currency">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CURRENCIES.map((c) => (
                <SelectItem key={c} value={c}>
                  {c}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Travellers" htmlFor="travelers" error={errors.travelers}>
          <NumberInput id="travelers" value={state.travelers} onChange={(v) => set("travelers", v ?? 1)} min={1} max={9} />
        </Field>
        <Field label="Cabin" htmlFor="cabin">
          <Select value={state.cabin_class} onValueChange={(v) => set("cabin_class", v as WizardState["cabin_class"])}>
            <SelectTrigger id="cabin">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="economy">Economy</SelectItem>
              <SelectItem value="premium_economy">Premium economy</SelectItem>
              <SelectItem value="business">Business</SelectItem>
              <SelectItem value="first">First</SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Field label="Max flight time (each way)" htmlFor="max_flight_hours">
          <NumberInput id="max_flight_hours" value={state.max_flight_hours} onChange={(v) => set("max_flight_hours", v ?? 30)} min={1} max={60} suffix="hours" />
        </Field>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex items-center justify-between gap-4 rounded-xl border p-4">
          <span>
            <span className="block text-sm font-medium">Direct flights only</span>
            <span className="text-xs text-muted-foreground">Rarely possible to remote surf spots.</span>
          </span>
          <Switch checked={state.direct_only} onCheckedChange={(v) => set("direct_only", v)} />
        </label>
        <Field label="Maximum layovers (each way)" htmlFor="max_layovers">
          <Select value={String(state.max_layovers)} onValueChange={(v) => set("max_layovers", Number(v))} disabled={state.direct_only}>
            <SelectTrigger id="max_layovers">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {[0, 1, 2, 3].map((n) => (
                <SelectItem key={n} value={String(n)}>
                  {n === 0 ? "Non-stop" : `${n} stop${n > 1 ? "s" : ""}`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
      </div>
      <Field label="Preferred airlines (optional)" htmlFor="preferred_airlines" error={errors.preferred_airlines} hint="2-letter IATA codes, comma separated. Preferred carriers rank higher; others still qualify.">
        <Input id="preferred_airlines" value={state.preferred_airlines} onChange={(e) => set("preferred_airlines", e.target.value)} placeholder="UA, QF" />
      </Field>

      <div>
        <Label className="mb-3">When several trips qualify, prioritise</Label>
        <RadioGroup value={state.priority} onValueChange={(v) => set("priority", v as WizardState["priority"])} className="grid gap-2 sm:grid-cols-4" aria-label="Priority">
          {[
            ["balanced", "Balanced", "Waves, price and convenience"],
            ["best_waves", "Best waves", "Quality first"],
            ["cheapest", "Cheapest", "Lowest fare"],
            ["shortest", "Shortest", "Least travel time"],
          ].map(([v, t, d]) => (
            <RadioCard key={v} value={v!} className="p-3">
              <span className="text-sm font-semibold">{t}</span>
              <span className="text-[11px] text-muted-foreground">{d}</span>
            </RadioCard>
          ))}
        </RadioGroup>
      </div>
    </div>
  );
}

function NotificationsStep({ state, set, user }: StepProps & { user: User }) {
  const smsReady = user.notification_preferences.sms_ready;
  return (
    <div className="space-y-6">
      <RadioGroup
        value={state.notification_channel}
        onValueChange={(v) => set("notification_channel", v as WizardState["notification_channel"])}
        className="grid gap-3 sm:grid-cols-2"
        aria-label="Notification channel"
      >
        <RadioCard value="email">
          <span className="font-semibold">Email</span>
          <span className="text-xs text-muted-foreground">Full details, charts link and flight info.</span>
        </RadioCard>
        <RadioCard value="sms" disabled={!smsReady}>
          <span className="font-semibold">SMS</span>
          <span className="text-xs text-muted-foreground">{smsReady ? "A short text with the essentials." : "Verify your phone in settings first."}</span>
        </RadioCard>
        <RadioCard value="both" disabled={!smsReady}>
          <span className="font-semibold">Email + SMS</span>
          <span className="text-xs text-muted-foreground">Never miss a swell.</span>
        </RadioCard>
        <RadioCard value="dashboard">
          <span className="font-semibold">Dashboard only</span>
          <span className="text-xs text-muted-foreground">No messages; check opportunities here.</span>
        </RadioCard>
      </RadioGroup>
      {!smsReady && (
        <p className="text-sm text-muted-foreground">
          Want SMS? <Link href="/settings" className="text-ocean hover:underline">Verify a phone number</Link> (opt-in, reply STOP any time).
        </p>
      )}
      {!user.email_verified && (state.notification_channel === "email" || state.notification_channel === "both") && (
        <Alert variant="warn">
          <AlertDescription>Email alerts start once you verify your email address.</AlertDescription>
        </Alert>
      )}
      <div className="space-y-3">
        <label className="flex items-center justify-between gap-4 rounded-xl border p-4">
          <span>
            <span className="block text-sm font-medium">Send updates</span>
            <span className="text-xs text-muted-foreground">Alert again if quality improves, the fare drops meaningfully, or dates shift.</span>
          </span>
          <Switch checked={state.notify_on_updates} onCheckedChange={(v) => set("notify_on_updates", v)} />
        </label>
        <label className="flex items-center justify-between gap-4 rounded-xl border p-4">
          <span>
            <span className="block text-sm font-medium">Surf-only alerts</span>
            <span className="text-xs text-muted-foreground">Also tell me about matching swells when no flight fits my budget.</span>
          </span>
          <Switch checked={state.notify_surf_only} onCheckedChange={(v) => set("notify_surf_only", v)} />
        </label>
      </div>
    </div>
  );
}

function ReviewStep({ state, set, errors = {}, goTo }: StepProps & { goTo: (i: number) => void }) {
  const input = toSearchInput(state);
  const sections: Array<{ step: StepKey; title: string; lines: string[] }> = [
    {
      step: "surf",
      title: "Surf",
      lines: [
        `${state.wave_min}–${state.wave_max} ${state.units} breaking faces${state.wave_preferred ? ` (ideal ${state.wave_preferred} ${state.units})` : ""}`,
        `${QUALITY_OPTIONS.find((q) => q.value === state.min_quality)?.label} or better, ≥ ${state.min_window_hours} h window`,
        [
          state.min_period_s ? `period ≥ ${state.min_period_s}s` : null,
          state.wind_requirement !== "any" ? state.wind_requirement.replace("_", " ") + " wind" : null,
          state.max_wind_kmh ? `wind ≤ ${state.max_wind_kmh} km/h` : null,
          state.break_types.length ? state.break_types.join(", ") : null,
        ]
          .filter(Boolean)
          .join(" · ") || "No extra wave filters",
      ],
    },
    {
      step: "destinations",
      title: "Destinations",
      lines: [
        state.destination_mode === "all"
          ? "All 50 monitored spots"
          : state.destination_mode === "regions"
            ? [...state.region_groups, ...state.countries].join(", ")
            : `${state.spot_slugs.length} selected spot${state.spot_slugs.length === 1 ? "" : "s"}`,
        state.max_transfer_minutes ? `Transfer ≤ ${state.max_transfer_minutes / 60} h from the airport` : "Any transfer time",
      ],
    },
    {
      step: "dates",
      title: "Dates",
      lines: [
        state.date_mode === "fixed" ? `${state.date_start} → ${state.date_end}` : `Flexible, next ${state.horizon_days} days`,
        `Arrive ${state.arrival_buffer_days} day(s) early, leave ${state.departure_buffer_days} day(s) after`,
        `${state.min_days_at_destination}–${state.max_trip_days} days at destination`,
      ],
    },
    {
      step: "flights",
      title: "Flights",
      lines: [
        `From ${state.origins.join(", ") || "—"}${state.max_origin_ground_km ? ` (+ airports within ${state.max_origin_ground_km} km)` : ""}`,
        `Up to ${state.max_price} ${state.currency} per traveller · ${state.travelers} traveller(s) · ${state.cabin_class.replace("_", " ")}`,
        `${state.direct_only ? "Direct only" : `≤ ${state.max_layovers} stops`} · ≤ ${state.max_flight_hours} h each way · priority: ${state.priority.replace("_", " ")}`,
      ],
    },
    {
      step: "notifications",
      title: "Notifications",
      lines: [
        `Channel: ${state.notification_channel}`,
        `${state.notify_on_updates ? "Updates on" : "No updates"} · ${state.notify_surf_only ? "surf-only alerts on" : "flight-qualified alerts only"}`,
      ],
    },
  ];
  return (
    <div className="space-y-6">
      <Field label="Search name" htmlFor="name" error={errors.name}>
        <Input id="name" value={state.name} onChange={(e) => set("name", e.target.value)} placeholder="e.g. Winter barrels from SFO" maxLength={120} aria-invalid={!!errors.name} />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        {sections.map((s) => (
          <div key={s.step} className="rounded-xl border p-4">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-sm font-semibold">{s.title}</span>
              <button type="button" className="text-xs text-ocean hover:underline" onClick={() => goTo(WIZARD_STEPS.findIndex((w) => w.key === s.step))}>
                Edit
              </button>
            </div>
            <ul className="space-y-1 text-sm text-muted-foreground">
              {s.lines.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        Monitoring runs on our servers every few hours, whether or not this page is open.{" "}
        <Badge variant="muted">{input.origins.length} origin(s)</Badge>
      </p>
    </div>
  );
}
