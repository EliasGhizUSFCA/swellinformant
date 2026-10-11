"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bell, MailWarning, Pause, Play, Plane, Plus, Search as SearchIcon, Waves } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { toast } from "sonner";

import { EventCard } from "@/components/event-card";
import { AppShell } from "@/components/layout/app-shell";
import { OpportunityCard } from "@/components/opportunity-card";
import { EmptyState, PageHeader, SectionTitle } from "@/components/page-header";
import { RequireAuth } from "@/components/require-auth";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { qk, useEvents, useNotifications, useOpportunities, useSearches, useVerificationStatus } from "@/hooks/queries";
import { errorMessage, post } from "@/lib/api";
import { deliveryStatus } from "@/lib/delivery";
import { formatDate, heightRange, relativeTime, titleCase, type Units } from "@/lib/format";
import { QUALITY_LABELS, asQuality } from "@/lib/quality";
import type { User } from "@/types/api";

function Stat({ icon, label, value, href }: { icon: React.ReactNode; label: string; value: React.ReactNode; href?: string }) {
  const body = (
    <Card className="h-full transition hover:shadow-md">
      <CardContent className="flex items-center gap-4 p-5">
        <div className="rounded-xl bg-secondary p-2.5 text-ocean [&_svg]:size-5">{icon}</div>
        <div>
          <div className="font-display text-2xl font-semibold text-ink tabular-nums">{value}</div>
          <div className="text-sm text-muted-foreground">{label}</div>
        </div>
      </CardContent>
    </Card>
  );
  return href ? <Link href={href}>{body}</Link> : body;
}

function VerifyBanner({ email }: { email: string }) {
  const queryClient = useQueryClient();
  const status = useVerificationStatus(true);
  const resend = useMutation({
    mutationFn: () => post<{ message: string }>("/api/auth/resend-verification"),
    onSuccess: (r) => {
      toast.success(r.message);
      void queryClient.invalidateQueries({ queryKey: qk.verification });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const s = status.data;
  const last = s?.last_email;
  const failed = last && ["failed", "bounced", "rejected", "complained"].includes(last.status);
  return (
    <Alert variant={failed ? "danger" : "warn"} className="mb-6" data-testid="verify-banner">
      <MailWarning />
      <AlertTitle>Verify your email to get alerts</AlertTitle>
      <AlertDescription className="space-y-2">
        <p>
          Trips still show up here. Email alerts to <strong>{email}</strong> start once it is verified.
        </p>
        {last && (
          <p className="flex flex-wrap items-center gap-2 text-sm" data-testid="verify-delivery">
            Latest verification email:
            <Badge variant={deliveryStatus(last.status).variant}>{deliveryStatus(last.status).label}</Badge>
            <span className="text-muted-foreground">
              {deliveryStatus(last.status).help} ({relativeTime(last.sent_at ?? last.created_at)})
            </span>
            {last.error && <span className="text-destructive">Reason: {last.error}</span>}
          </p>
        )}
        {s?.delivery_mode === "capture" && (
          <p className="text-sm" data-testid="dev-inbox-hint">
            Development mode: emails are captured, not sent.{" "}
            {s.dev_inbox_url ? (
              <a href={s.dev_inbox_url} target="_blank" rel="noreferrer" className="font-medium text-ocean underline">
                Open the dev inbox
              </a>
            ) : (
              <>
                Find the message in <code className="rounded bg-muted px-1">{s.captured_at}</code>
              </>
            )}{" "}
            to get your link.
          </p>
        )}
        {s?.delivery_mode === "live" && !failed && (
          <p className="text-sm text-muted-foreground">Not there after a few minutes? Check spam, then resend.</p>
        )}
        <Button size="sm" variant="outline" onClick={() => resend.mutate()} disabled={resend.isPending}>
          Resend link
        </Button>
      </AlertDescription>
    </Alert>
  );
}

function Welcome() {
  const welcome = useSearchParams().get("welcome");
  if (!welcome) return null;
  return (
    <Alert variant="info" className="mb-6">
      <Waves />
      <AlertTitle>Welcome aboard!</AlertTitle>
      <AlertDescription>Create a search with the waves you want and where you fly from. We&apos;ll do the watching.</AlertDescription>
    </Alert>
  );
}

function Dashboard({ user }: { user: User }) {
  const units = (user.profile.units as Units) ?? "ft";
  const queryClient = useQueryClient();
  const searches = useSearches();
  const opportunities = useOpportunities();
  const notifications = useNotifications(0, 5);
  const events = useEvents(6, 57);

  const toggle = useMutation({
    mutationFn: ({ id, paused }: { id: string; paused: boolean }) => post(`/api/searches/${id}/${paused ? "resume" : "pause"}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.searches });
      toast.success("Search updated.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const opps = opportunities.data ?? [];
  const withFlights = opps.filter((o) => o.status === "flight_found");
  const surfOnly = opps.filter((o) => o.status !== "flight_found");
  const activeSearches = (searches.data ?? []).filter((s) => s.status === "active").length;

  return (
    <>
      <PageHeader
        eyebrow="Dashboard"
        title={`Hi ${user.full_name.split(" ")[0]} — here's the swell outlook`}
        description="Updates automatically with every new forecast, even while this page is closed."
        actions={
          <Button asChild>
            <Link href="/searches/new">
              <Plus /> New search
            </Link>
          </Button>
        }
      />
      <Suspense>
        <Welcome />
      </Suspense>
      {!user.email_verified && <VerifyBanner email={user.email} />}

      <div className="mb-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat icon={<SearchIcon />} label="Active searches" value={searches.isLoading ? "–" : activeSearches} href="/searches" />
        <Stat icon={<Plane />} label="Trips with flights" value={opportunities.isLoading ? "–" : withFlights.length} />
        <Stat icon={<Waves />} label="Surf-only watches" value={opportunities.isLoading ? "–" : surfOnly.length} />
        <Stat icon={<Bell />} label="Alerts sent" value={notifications.data?.total ?? "–"} href="/notifications" />
      </div>

      <div className="grid gap-8 lg:grid-cols-[1.6fr_1fr]">
        <section data-testid="current-opportunities">
          <SectionTitle>Your surf trips</SectionTitle>
          {opportunities.isLoading && <Skeleton className="h-36" />}
          {!opportunities.isLoading && opps.length === 0 && (
            <EmptyState
              icon={<Waves />}
              title="No opportunities yet"
              description={
                (searches.data ?? []).length === 0
                  ? "Create a search and we'll start matching swells to your preferences."
                  : "Your searches are active. Matching swells appear here 5–10 days out."
              }
              action={
                (searches.data ?? []).length === 0 ? (
                  <Button asChild>
                    <Link href="/searches/new">Create a search</Link>
                  </Button>
                ) : undefined
              }
            />
          )}
          <div className="space-y-3">
            {opps.map((m) => (
              <OpportunityCard key={m.id} match={m} units={units} />
            ))}
          </div>

          <div className="mt-10">
            <SectionTitle
              action={
                <Link href="/map" className="text-sm text-ocean hover:underline">
                  Open surf map
                </Link>
              }
            >
              Top surf forecasts
            </SectionTitle>
            {events.isLoading && <Skeleton className="h-40" />}
            <div className="grid gap-3 sm:grid-cols-2" data-testid="top-events">
              {(events.data ?? []).map((e, i) => (
                <div key={e.id} className={i === 0 ? "sm:col-span-2" : undefined}>
                  <EventCard event={e} units={units} featured={i === 0} />
                </div>
              ))}
            </div>
            {events.data?.length === 0 && <p className="text-sm text-muted-foreground">No Good-or-better swells in the current forecast.</p>}
          </div>
        </section>

        <aside className="space-y-6">
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Active searches</CardTitle>
              <Link href="/searches" className="text-sm text-ocean hover:underline">
                Manage
              </Link>
            </CardHeader>
            <CardContent className="space-y-3">
              {searches.isLoading && <Skeleton className="h-16" />}
              {(searches.data ?? []).length === 0 && !searches.isLoading && (
                <p className="text-sm text-muted-foreground">No searches yet.</p>
              )}
              {(searches.data ?? []).slice(0, 5).map((s) => (
                <div key={s.id} className="flex items-center justify-between gap-3 rounded-xl border p-3">
                  <div className="min-w-0">
                    <Link href={`/searches/${s.id}`} className="block truncate font-medium hover:underline">
                      {s.name}
                    </Link>
                    <div className="text-xs text-muted-foreground">
                      {heightRange(s.wave_min_ft, s.wave_max_ft, units)} breaking · {QUALITY_LABELS[asQuality(s.min_quality)]}+ · from{" "}
                      {s.origins.join(", ")}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant={s.status === "active" ? "good" : "muted"}>{s.status}</Badge>
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={s.status === "active" ? `Pause ${s.name}` : `Resume ${s.name}`}
                      onClick={() => toggle.mutate({ id: s.id, paused: s.status !== "active" })}
                    >
                      {s.status === "active" ? <Pause /> : <Play />}
                    </Button>
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Recent alerts</CardTitle>
              <Link href="/notifications" className="text-sm text-ocean hover:underline">
                History
              </Link>
            </CardHeader>
            <CardContent className="space-y-3">
              {(notifications.data?.items ?? []).length === 0 && <p className="text-sm text-muted-foreground">No alerts yet.</p>}
              {(notifications.data?.items ?? []).map((n) => (
                <Link
                  key={n.id}
                  href={n.match_id ? `/opportunities/${n.match_id}` : "/notifications"}
                  className="block rounded-xl border p-3 hover:bg-muted/50"
                >
                  <div className="truncate text-sm font-medium">{n.subject}</div>
                  <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                    <Badge variant={deliveryStatus(n.status).variant} title={deliveryStatus(n.status).help}>
                      {deliveryStatus(n.status).label}
                    </Badge>
                    {titleCase(n.channel)} · {relativeTime(n.created_at)}
                  </div>
                </Link>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Saved preferences</CardTitle>
              <Link href="/settings" className="text-sm text-ocean hover:underline">
                Edit
              </Link>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <Row k="Home airport" v={user.profile.home_airport ?? "Not set"} />
              <Row k="Units" v={units === "m" ? "Metres" : "Feet"} />
              <Row
                k="Email alerts"
                v={
                  user.notification_preferences.all_paused
                    ? "Paused"
                    : user.notification_preferences.email_enabled
                      ? user.email_verified
                        ? "On"
                        : "Waiting for verification"
                      : "Off"
                }
              />
              <Row k="SMS alerts" v={user.notification_preferences.sms_ready ? "On" : "Not set up"} />
              <Row k="Member since" v={formatDate(user.created_at, "UTC", { year: "numeric" })} />
            </CardContent>
          </Card>
        </aside>
      </div>
    </>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4">
      <span className="text-muted-foreground">{k}</span>
      <span className="font-medium">{v}</span>
    </div>
  );
}

export default function DashboardPage() {
  return (
    <AppShell>
      <RequireAuth>{(user) => <Dashboard user={user} />}</RequireAuth>
    </AppShell>
  );
}
