"use client";

import { Bell, ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { AppShell } from "@/components/layout/app-shell";
import { EmptyState, PageHeader } from "@/components/page-header";
import { DemoBadge, MatchStatusBadge, QualityBadge } from "@/components/quality";
import { RequireAuth } from "@/components/require-auth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useNotifications } from "@/hooks/queries";
import { deliveryStatus } from "@/lib/delivery";
import { formatDateTime, formatMoney, titleCase } from "@/lib/format";

const PAGE = 25;

function History() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading } = useNotifications(offset, PAGE);
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (isLoading) return <Skeleton className="h-96" />;
  if (!data || data.total === 0)
    return <EmptyState icon={<Bell />} title="No alerts yet" description="When a swell matches one of your searches, the alert will be recorded here." />;
  return (
    <>
      <div className="overflow-x-auto rounded-2xl border bg-card shadow-sm">
        <table className="w-full min-w-[860px] text-sm" data-testid="notification-table">
          <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-4 py-3 font-medium">Generated</th>
              <th className="px-4 py-3 font-medium">Alert</th>
              <th className="px-4 py-3 font-medium">Channel</th>
              <th className="px-4 py-3 font-medium">Delivery</th>
              <th className="px-4 py-3 font-medium">Fare at alert</th>
              <th className="px-4 py-3 font-medium">Now</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((n) => (
              <tr key={n.id} className="border-t align-top">
                <td className="px-4 py-3 whitespace-nowrap text-muted-foreground">{formatDateTime(n.created_at, tz)}</td>
                <td className="px-4 py-3">
                  <div className="font-medium">
                    {n.match_id ? (
                      <Link href={`/opportunities/${n.match_id}`} className="hover:underline">
                        {n.subject}
                      </Link>
                    ) : (
                      n.subject
                    )}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <Badge variant="secondary">{titleCase(n.kind)}</Badge>
                    {n.peak_label && <QualityBadge label={n.peak_label} />}
                    {n.window_label && <span>{n.window_label}</span>}
                    {n.is_demo && <DemoBadge />}
                  </div>
                </td>
                <td className="px-4 py-3">
                  {titleCase(n.channel)}
                  <div className="text-xs text-muted-foreground">{n.recipient_masked}</div>
                </td>
                <td className="px-4 py-3" data-testid="delivery-cell">
                  <Badge variant={deliveryStatus(n.status).variant} title={deliveryStatus(n.status).help}>
                    {deliveryStatus(n.status).label}
                  </Badge>
                  <div className="mt-1 max-w-[220px] text-xs text-muted-foreground">{deliveryStatus(n.status).help}</div>
                  {n.sent_at && (
                    <div className="mt-1 text-xs text-muted-foreground">
                      {n.status === "captured" ? "Captured" : "Handed to"} {n.provider ?? "provider"} · {formatDateTime(n.sent_at, tz)}
                    </div>
                  )}
                  {n.delivered_at && <div className="mt-1 text-xs text-emerald-700">Delivered {formatDateTime(n.delivered_at, tz)}</div>}
                  {n.last_error && !["accepted", "captured", "delivered"].includes(n.status) && (
                    <div className={`mt-1 max-w-[220px] text-xs ${n.status === "suppressed" ? "text-amber-800" : "text-destructive"}`}>{n.last_error}</div>
                  )}
                </td>
                <td className="px-4 py-3 whitespace-nowrap">{n.price ? formatMoney(n.price, n.currency) : "—"}</td>
                <td className="px-4 py-3">
                  {n.match_status ? <MatchStatusBadge status={n.match_status} /> : <span className="text-xs text-muted-foreground">removed</span>}
                  {n.event_status && <div className="mt-1 text-xs text-muted-foreground">swell {n.event_status}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-4 flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}
        </span>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            <ChevronLeft /> Newer
          </Button>
          <Button variant="outline" size="sm" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>
            Older <ChevronRight />
          </Button>
        </div>
      </div>
    </>
  );
}

export default function NotificationsPage() {
  return (
    <AppShell>
      <PageHeader eyebrow="Alerts" title="Notification history" description="Every alert we generated, how it was delivered, and how the opportunity looks now." />
      <RequireAuth>{() => <History />}</RequireAuth>
    </AppShell>
  );
}
