"use client";

import { Search } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { ConfidenceBadge } from "@/components/confidence";
import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { QualityBadge } from "@/components/quality";
import { StaleLine } from "@/components/stale-banner";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useMe, useSpots } from "@/hooks/queries";
import { freshnessOf } from "@/lib/forecast-fields";
import { formatDate, formatRange, heightRange, type Units } from "@/lib/format";
import { useHeightUnit } from "@/lib/units";

export default function SpotsPage() {
  const { data: spots = [], isLoading } = useSpots();
  const { data: user } = useMe();
  // The site-wide ft/m choice (signed-out visitors too), else the account's units.
  const [units] = useHeightUnit((user?.profile.units as Units | undefined) ?? "ft");
  const [q, setQ] = useState("");
  const grouped = useMemo(() => {
    const f = q.trim().toLowerCase();
    const m = new Map<string, typeof spots>();
    for (const s of spots) {
      if (f && !`${s.name} ${s.country} ${s.region} ${s.break_type}`.toLowerCase().includes(f)) continue;
      m.set(s.region_group, [...(m.get(s.region_group) ?? []), s]);
    }
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [spots, q]);

  return (
    <AppShell>
      <PageHeader
        eyebrow="Monitored spots"
        title="50 breaks, watched around the clock"
        description="A curated list of well-known waves, not a ranking. Ratings show the best surf in the next 7 days."
        actions={
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input placeholder="Search spots" value={q} onChange={(e) => setQ(e.target.value)} className="w-full pl-9 sm:w-64" aria-label="Search spots" />
          </div>
        }
      />
      {isLoading && <Skeleton className="h-96" />}
      <div className="space-y-10">
        {grouped.map(([group, list]) => (
          <section key={group}>
            <h2 className="mb-3 text-sm font-semibold tracking-[0.14em] text-ocean uppercase">{group}</h2>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {list.map((s) => {
                const fresh = freshnessOf(s);
                return (
                  <Link
                    key={s.slug}
                    href={`/spots/${s.slug}`}
                    className="flex min-w-0 flex-col rounded-2xl border bg-card p-4 shadow-sm transition hover:-translate-y-0.5 hover:shadow-md"
                    data-testid="spot-card"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate font-semibold text-ink">{s.name}</div>
                        <div className="text-xs text-muted-foreground">
                          {s.region} · {s.country}
                        </div>
                      </div>
                      {s.best_upcoming && <QualityBadge label={s.best_upcoming.label} score={s.best_upcoming.score} />}
                    </div>
                    <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      <span className="capitalize">{s.break_type}</span>
                      <span className="capitalize">{s.wave_direction}</span>
                      <span className="capitalize">{s.skill_level}</span>
                      <span>works {heightRange(s.wave_height_min_ft, s.wave_height_max_ft, units)}</span>
                      {s.is_big_wave && <span className="font-medium text-coral">big wave</span>}
                    </div>
                    {s.best_upcoming && (
                      <div className="mt-2 text-xs text-ink">
                        Best: {heightRange(s.best_upcoming.breaking_height_min_ft, s.best_upcoming.breaking_height_max_ft, units)} breaking ·{" "}
                        {formatDate(s.best_upcoming.valid_time, s.timezone, { weekday: "short" })}
                      </div>
                    )}
                    {/* Never shown as current: the rating above is from an out-of-date forecast. */}
                    <StaleLine freshness={fresh} className="mt-2" data-testid="spot-card-stale" />
                    {s.next_event && (
                      <div className="mt-1 text-xs text-ink">
                        Next swell: {formatRange(s.next_event.start_time, s.next_event.end_time, s.timezone)} ·{" "}
                        {heightRange(s.next_event.peak_breaking_height_min_ft, s.next_event.peak_breaking_height_max_ft, units)}
                      </div>
                    )}
                    <div className="mt-auto flex flex-wrap gap-1.5 pt-3">
                      {s.best_upcoming && <ConfidenceBadge value={s.best_upcoming.forecast_confidence_class} />}
                      <ConfidenceBadge value={s.research_class} kind="research" label="Research confidence" />
                    </div>
                  </Link>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </AppShell>
  );
}
