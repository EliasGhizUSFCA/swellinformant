"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Copy, History, MoreHorizontal, Pause, Pencil, Play, Plus, Search as SearchIcon, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { AppShell } from "@/components/layout/app-shell";
import { EmptyState, PageHeader } from "@/components/page-header";
import { RequireAuth } from "@/components/require-auth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { qk, useSearches } from "@/hooks/queries";
import { del, errorMessage, post } from "@/lib/api";
import { formatCalendarDate, formatMoney, heightRange, relativeTime, type Units } from "@/lib/format";
import { asQuality, QUALITY_LABELS } from "@/lib/quality";
import type { Search, User } from "@/types/api";

function destinationSummary(s: Search): string {
  if (s.destination_mode === "all") return "All 50 spots";
  if (s.destination_mode === "regions") return [...(s.destinations.region_groups ?? []), ...(s.destinations.countries ?? [])].join(", ");
  const n = s.destinations.spot_slugs?.length ?? 0;
  return `${n} spot${n === 1 ? "" : "s"}`;
}

function SearchRow({ s, units }: { s: Search; units: Units }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: qk.searches });
  const action = useMutation({
    mutationFn: (kind: "pause" | "resume" | "duplicate") => post<Search>(`/api/searches/${s.id}/${kind}`),
    onSuccess: (res, kind) => {
      refresh();
      if (kind === "duplicate") {
        toast.success("Copy created (paused). Adjust it, then resume.");
        router.push(`/searches/${res.id}/edit`);
      } else toast.success(kind === "pause" ? "Search paused — no new alerts will be sent." : "Search resumed.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => del(`/api/searches/${s.id}`),
    onSuccess: () => {
      refresh();
      setConfirm(false);
      toast.success("Search deleted.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const active = s.status === "active";
  return (
    <Card data-testid="search-row">
      <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Link href={`/searches/${s.id}`} className="text-base font-semibold text-ink hover:underline">
              {s.name}
            </Link>
            <Badge variant={active ? "good" : "muted"} data-testid="search-status">
              {active ? "Active" : "Paused"}
            </Badge>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
            <span>{heightRange(s.wave_min_ft, s.wave_max_ft, units)}</span>
            <span>{QUALITY_LABELS[asQuality(s.min_quality)]}+</span>
            <span>{destinationSummary(s)}</span>
            <span>from {s.origins.join(", ")}</span>
            <span>≤ {formatMoney(s.travel.max_price, s.travel.currency)}</span>
            <span>{s.date_mode === "fixed" ? `${formatCalendarDate(s.date_start)} – ${formatCalendarDate(s.date_end)}` : `next ${s.horizon_days} days`}</span>
          </div>
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            <Badge variant="good">{s.match_counts.flight_found} with flights</Badge>
            <Badge variant="warn">{s.match_counts.surf_only} surf-only</Badge>
            {s.match_counts.pending_flights > 0 && <Badge variant="info">{s.match_counts.pending_flights} checking flights</Badge>}
            <span className="text-muted-foreground">
              {s.last_evaluated_at ? `Checked ${relativeTime(s.last_evaluated_at)}` : "Waiting for first evaluation"}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => action.mutate(active ? "pause" : "resume")} disabled={action.isPending}>
            {active ? <Pause /> : <Play />} {active ? "Pause" : "Resume"}
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" aria-label={`More actions for ${s.name}`}>
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => router.push(`/searches/${s.id}/edit`)}>
                <Pencil /> Edit
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => router.push(`/searches/${s.id}`)}>
                <History /> Matching history
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => action.mutate("duplicate")}>
                <Copy /> Duplicate
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem className="text-destructive" onSelect={() => setConfirm(true)}>
                <Trash2 /> Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </CardContent>
      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{s.name}”?</DialogTitle>
            <DialogDescription>Its opportunities are removed too. Alerts already sent stay in your history.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirm(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => remove.mutate()} disabled={remove.isPending}>
              Delete search
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Searches({ user }: { user: User }) {
  const { data, isLoading } = useSearches();
  const units = (user.profile.units as Units) ?? "ft";
  return (
    <>
      <PageHeader
        eyebrow="Searches"
        title="Your swell searches"
        description="Each search is evaluated automatically against every new forecast run."
        actions={
          <Button asChild>
            <Link href="/searches/new">
              <Plus /> New search
            </Link>
          </Button>
        }
      />
      {isLoading && <Skeleton className="h-32" />}
      {data && data.length === 0 && (
        <EmptyState
          icon={<SearchIcon />}
          title="No searches yet"
          description="Tell us the waves you want, where you fly from and your budget."
          action={
            <Button asChild>
              <Link href="/searches/new">Create your first search</Link>
            </Button>
          }
        />
      )}
      <div className="space-y-3">
        {(data ?? []).map((s) => (
          <SearchRow key={s.id} s={s} units={units} />
        ))}
      </div>
    </>
  );
}

export default function SearchesPage() {
  return (
    <AppShell>
      <RequireAuth>{(user) => <Searches user={user} />}</RequireAuth>
    </AppShell>
  );
}
