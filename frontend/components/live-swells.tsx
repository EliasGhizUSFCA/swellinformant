"use client";

import Link from "next/link";

import { EventCard } from "@/components/event-card";
import { Skeleton } from "@/components/ui/skeleton";
import { useEvents } from "@/hooks/queries";

/** Upcoming swell events from the live system (public), shown on the landing page. */
export function LiveSwells() {
  const { data, isLoading, isError } = useEvents(4, 57);
  return (
    <div className="rounded-3xl border border-white/15 bg-white/[0.04] p-4 backdrop-blur-sm sm:p-5">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold tracking-wide text-white/90">Swells on the radar</h2>
        <span className="flex items-center gap-1.5 text-xs text-white/60">
          <span className="size-1.5 animate-pulse rounded-full bg-seafoam" /> live from the forecast pipeline
        </span>
      </div>
      {isLoading && (
        <div className="grid gap-3 sm:grid-cols-2">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-44 bg-white/10" />
          ))}
        </div>
      )}
      {isError && <p className="py-10 text-center text-sm text-white/70">The forecast service is unavailable right now.</p>}
      {data && data.length === 0 && (
        <p className="py-10 text-center text-sm text-white/70">
          No qualifying swells in the current forecast. Check the <Link className="underline" href="/map">surf map</Link>.
        </p>
      )}
      {data && data.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {data.map((e) => (
            <EventCard key={e.id} event={e} dark />
          ))}
        </div>
      )}
    </div>
  );
}
