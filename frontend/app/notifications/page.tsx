"use client";

import { Bell, ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ConfidenceBadge } from "@/components/confidence";
import { InfoPopover } from "@/components/info-popover";
import { AppShell } from "@/components/layout/app-shell";
import { EmptyState, PageHeader } from "@/components/page-header";
import { DemoBadge, MatchStatusBadge, QualityBadge } from "@/components/quality";
import { RequireAuth } from "@/components/require-auth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useNotifications } from "@/hooks/queries";
import { hasField } from "@/lib/confidence";
import { deliveryStatus } from "@/lib/delivery";
import { formatDateTime, formatMoney, titleCase } from "@/lib/format";

const PAGE = 25;

/**
 * Alert history. On small screens each row stacks into a card (cells keep explicit table
 * roles so the table stays a table for assistive technology); on wider screens it is a table.
 */
function History() {
  const [offset, setOffset] = useState(0);
  const { data, isLoading } = useNotifications(offset, PAGE);
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (isLoading) return <Skeleton className="h-96" />;
  if (!data || data.total === 0)
    return <EmptyState icon={<Bell />} title="No alerts yet" description="When a swell matches one of your searches, the alert is recorded here." />;
  const cell = "block px-4 py-1.5 md:table-cell md:py-3";
  const label = "mr-2 text-xs text-muted-foreground md:hidden";
  return (
    <>
      <div className="rounded-2xl border bg-card shadow-sm md:overflow-x-auto">
        <table className="block w-full text-sm md:table md:min-w-[760px]" data-testid="notification-table" role="table">
          <thead className="sr-only bg-muted/60 text-left text-xs text-muted-foreground md:not-sr-only md:table-header-group" role="rowgroup">
            <tr role="row">
              <th className="px-4 py-3 font-medium whitespace-nowrap" role="columnheader">
                Generated
              </th>
              <th className="px-4 py-3 font-medium whitespace-nowrap" role="columnheader">
                Alert
              </th>
              <th className="px-4 py-3 font-medium whitespace-nowrap" role="columnheader">
                Delivery
              </th>
              <th className="px-4 py-3 font-medium whitespace-nowrap" role="columnheader">
                Fare at alert
              </th>
              <th className="px-4 py-3 font-medium whitespace-nowrap" role="columnheader">
                Trip now
              </th>
            </tr>
          </thead>
          <tbody className="block md:table-row-group" role="rowgroup">
            {data.items.map((n) => {
              const d = deliveryStatus(n.status);
              return (
                <tr key={n.id} className="block border-t py-2 align-top first:border-t-0 md:table-row md:py-0 md:first:border-t" role="row">
                  <td className={`${cell} whitespace-nowrap text-xs text-muted-foreground md:text-sm`} role="cell">
                    {formatDateTime(n.created_at, tz)}
                  </td>
                  <td className={cell} role="cell">
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
                      {hasField(n, "forecast_confidence_class") && <ConfidenceBadge value={n.forecast_confidence_class} />}
                      {n.window_label && <span>{n.window_label}</span>}
                      {n.is_demo && <DemoBadge />}
                    </div>
                  </td>
                  <td className={cell} data-testid="delivery-cell" role="cell">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant={d.variant} title={d.help}>
                        {d.label}
                      </Badge>
                      {d.help && (
                        <InfoPopover title={d.label}>
                          <p>{d.help}</p>
                        </InfoPopover>
                      )}
                      <span className="text-xs text-muted-foreground">
                        {titleCase(n.channel)} · {n.recipient_masked}
                      </span>
                    </div>
                    {n.sent_at && (
                      <div className="mt-1 text-xs text-muted-foreground">
                        {n.status === "captured" ? "Captured" : "Handed to"} {n.provider ?? "provider"} · {formatDateTime(n.sent_at, tz)}
                      </div>
                    )}
                    {n.delivered_at && <div className="mt-1 text-xs text-emerald-700">Delivered {formatDateTime(n.delivered_at, tz)}</div>}
                    {n.last_error && !["accepted", "captured", "delivered"].includes(n.status) && (
                      <div className={`mt-1 max-w-[260px] text-xs ${n.status === "suppressed" ? "text-amber-800" : "text-destructive"}`}>{n.last_error}</div>
                    )}
                  </td>
                  <td className={`${cell} whitespace-nowrap`} role="cell">
                    <span className={label}>Fare at alert</span>
                    {n.price ? formatMoney(n.price, n.currency) : "—"}
                  </td>
                  <td className={cell} role="cell">
                    <span className={label}>Trip now</span>
                    {n.match_status ? <MatchStatusBadge status={n.match_status} /> : <span className="text-xs text-muted-foreground">removed</span>}
                    {n.event_status && <div className="mt-1 text-xs text-muted-foreground md:block">swell {n.event_status}</div>}
                  </td>
                </tr>
              );
            })}
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
      <PageHeader eyebrow="Alerts" title="Notification history" description="Every alert we generated, how it was delivered and how the trip looks now." />
      <RequireAuth>{() => <History />}</RequireAuth>
    </AppShell>
  );
}
