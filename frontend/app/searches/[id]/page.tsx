"use client";

import { Pencil } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { AppShell } from "@/components/layout/app-shell";
import { OpportunityCard } from "@/components/opportunity-card";
import { EmptyState, PageHeader, SectionTitle } from "@/components/page-header";
import { MatchStatusBadge, QualityBadge } from "@/components/quality";
import { RequireAuth } from "@/components/require-auth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useSearch, useSearchMatches } from "@/hooks/queries";
import { formatCalendarDate, formatMoney, formatRange, heightRange, titleCase, type Units } from "@/lib/format";
import { asQuality, QUALITY_LABELS } from "@/lib/quality";
import type { User } from "@/types/api";

function Detail({ user, id }: { user: User; id: string }) {
  const units = (user.profile.units as Units) ?? "ft";
  const search = useSearch(id);
  const matches = useSearchMatches(id);
  if (search.isLoading) return <Skeleton className="h-96" />;
  const s = search.data;
  if (!s) return <p className="text-muted-foreground">Search not found.</p>;
  const current = (matches.data ?? []).filter((m) => ["flight_found", "surf_only", "pending_flights"].includes(m.status));
  const history = (matches.data ?? []).filter((m) => !["flight_found", "surf_only", "pending_flights"].includes(m.status));
  const rows: Array<[string, React.ReactNode]> = [
    ["Waves", `${heightRange(s.wave_min_ft, s.wave_max_ft, units)} · ${QUALITY_LABELS[asQuality(s.min_quality)]}+`],
    ["Wind", titleCase(s.wind_requirement)],
    ["Destinations", s.destination_mode === "all" ? "All spots" : s.destination_mode === "regions" ? [...(s.destinations.region_groups ?? []), ...(s.destinations.countries ?? [])].join(", ") : (s.destinations.spot_slugs ?? []).join(", ")],
    ["Dates", s.date_mode === "fixed" ? `${formatCalendarDate(s.date_start)} – ${formatCalendarDate(s.date_end)}` : `Flexible, next ${s.horizon_days} days`],
    ["Buffers", `Arrive ${s.travel.arrival_buffer_days}d early · leave ${s.travel.departure_buffer_days}d after`],
    ["Flights", `From ${s.origins.join(", ")} · ≤ ${formatMoney(s.travel.max_price, s.travel.currency)} pp · ${s.travel.travelers} traveller(s)`],
    ["Alerts", `${titleCase(s.notification_channel)}${s.notify_on_updates ? " · updates" : ""}${s.notify_surf_only ? " · surf-only" : ""}`],
  ];
  return (
    <>
      <PageHeader
        eyebrow="Search"
        title={s.name}
        description={<Badge variant={s.status === "active" ? "good" : "muted"}>{s.status === "active" ? "Active" : "Paused"}</Badge>}
        actions={
          <Button asChild variant="outline">
            <Link href={`/searches/${id}/edit`}>
              <Pencil /> Edit
            </Link>
          </Button>
        }
      />
      <div className="grid gap-8 lg:grid-cols-[1.6fr_1fr]">
        <section>
          <SectionTitle>Current matches</SectionTitle>
          {matches.isLoading && <Skeleton className="h-32" />}
          {!matches.isLoading && current.length === 0 && (
            <EmptyState title="No current matches" description="We'll add them here when a swell meets these criteria 5–10 days out." />
          )}
          <div className="space-y-3">
            {current.map((m) => (
              <OpportunityCard key={m.id} match={m} units={units} />
            ))}
          </div>
          <div className="mt-10">
            <SectionTitle>Matching history</SectionTitle>
            {history.length === 0 ? (
              <p className="text-sm text-muted-foreground">No past or expired matches.</p>
            ) : (
              <div className="overflow-hidden rounded-2xl border bg-card">
                <table className="w-full text-sm">
                  <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
                    <tr>
                      <th className="px-4 py-2 font-medium">Spot</th>
                      <th className="px-4 py-2 font-medium">Window</th>
                      <th className="px-4 py-2 font-medium">Quality</th>
                      <th className="px-4 py-2 font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((m) => (
                      <tr key={m.id} className="border-t">
                        <td className="px-4 py-2">
                          <Link href={`/opportunities/${m.id}`} className="hover:underline">
                            {m.spot.name}
                          </Link>
                        </td>
                        <td className="px-4 py-2">{formatRange(m.window_start, m.window_end, m.spot.timezone)}</td>
                        <td className="px-4 py-2">
                          <QualityBadge label={m.peak_label} />
                        </td>
                        <td className="px-4 py-2">
                          <MatchStatusBadge status={m.status} />
                          {m.status_reason && <div className="mt-1 text-xs text-muted-foreground">{m.status_reason}</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </section>
        <Card className="h-fit">
          <CardHeader>
            <CardTitle>Criteria</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {rows.map(([k, v]) => (
              <div key={k}>
                <div className="text-xs text-muted-foreground">{k}</div>
                <div className="font-medium">{v}</div>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </>
  );
}

export default function SearchDetailPage() {
  const { id } = useParams<{ id: string }>();
  return (
    <AppShell>
      <RequireAuth>{(user) => <Detail user={user} id={id} />}</RequireAuth>
    </AppShell>
  );
}
