import { ArrowRight, BellRing, CalendarClock, Compass, Gauge, Plane, Radar, ShieldCheck, Waves } from "lucide-react";
import Link from "next/link";

import { Logo } from "@/components/brand/logo";
import { SiteFooter } from "@/components/layout/site-footer";
import { ForecastHorizon, LiveSwells } from "@/components/live-swells";
import { Button } from "@/components/ui/button";

const STEPS = [
  {
    icon: Gauge,
    title: "Tell us your waves",
    body: "Set the size, quality, wind and break types you want, where you'll fly from and what you'll spend.",
  },
  {
    icon: Radar,
    title: "We watch 50 breaks around the clock",
    body: "Wind and wave forecasts are scored for every break at each forecast time step, and refreshed as soon as a new model run is published.",
  },
  {
    icon: Plane,
    title: "A swell lines up, we price the trip",
    body: "When a qualifying swell falls inside your alert lead time (2–10 days out by default, swells 3–7 days ahead first) we search flights that land before it and leave after it.",
  },
  {
    icon: BellRing,
    title: "You get a ready-made trip",
    body: "Email or SMS with the surf window, expected size, confidence, fare and arrival/departure dates.",
  },
];

const FEATURES = [
  {
    icon: CalendarClock,
    title: "Arrival & departure buffers",
    body: "Arrive two days early, leave one day after — computed from real itinerary timestamps across time zones and the date line.",
  },
  {
    icon: Compass,
    title: "Spot-specific scoring",
    body: "Swell window, period, offshore wind, tide and size range for each break, rated out of 10 with the reasons shown.",
  },
  {
    icon: ShieldCheck,
    title: "Honest uncertainty",
    body: "Forecast confidence is shown separately from quality and from how well each break is researched. Breaking heights are labelled as estimates, fares as quotes.",
  },
  {
    icon: Waves,
    title: "Many searches at once",
    body: "Run a cheap-weekend-at-Trestles search alongside a once-a-year Mentawai mission. Pause any time.",
  },
];

export default function LandingPage() {
  return (
    <div className="flex min-h-dvh flex-col bg-ink">
      <section className="relative overflow-hidden bg-ocean-gradient text-white">
        <header className="relative z-10 mx-auto flex h-20 max-w-7xl items-center justify-between px-4 sm:px-6">
          <Logo light />
          <nav className="flex items-center gap-2">
            {/* Phones: the hero's "Explore the forecast map" button links the map, so the header
                keeps only the account actions and fits the width. */}
            <Button asChild variant="ghost" className="hidden text-white hover:bg-white/10 hover:text-white sm:inline-flex">
              <Link href="/map">Surf map</Link>
            </Button>
            <Button asChild variant="ghost" className="hidden text-white hover:bg-white/10 hover:text-white md:inline-flex">
              <Link href="/how-to-use">How to use</Link>
            </Button>
            <Button asChild variant="ghost" className="text-white hover:bg-white/10 hover:text-white">
              <Link href="/login">Sign in</Link>
            </Button>
            <Button asChild variant="coral">
              <Link href="/register">Get started</Link>
            </Button>
          </nav>
        </header>

        <div className="relative z-10 mx-auto grid max-w-7xl gap-12 px-4 pt-12 pb-40 sm:px-6 lg:grid-cols-[1.1fr_1fr] lg:pt-20">
          <div>
            <p className="mb-5 inline-flex items-center gap-2 rounded-full border border-white/20 bg-white/10 px-3 py-1 text-xs font-medium tracking-wide text-white/85">
              <span className="size-1.5 rounded-full bg-seafoam" /> Find the swell, not just the destination
            </p>
            <h1 className="font-display text-5xl leading-[1.02] font-semibold text-balance sm:text-6xl lg:text-7xl">
              Chase the Swell.
              <br />
              <span className="text-seafoam">We&apos;ll Find the Flight.</span>
            </h1>
            <p className="mt-6 max-w-xl text-lg text-white/80">
              Find world-class surf and affordable flights automatically. Instead of choosing a destination and hoping
              for waves, let the waves choose the destination.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Button asChild size="lg" variant="coral">
                <Link href="/register">
                  Start a swell search <ArrowRight />
                </Link>
              </Button>
              <Button asChild size="lg" variant="glass">
                <Link href="/map">Explore the forecast map</Link>
              </Button>
            </div>
            <dl className="mt-12 grid max-w-lg grid-cols-3 gap-6 border-t border-white/15 pt-6">
              <div>
                <dt className="font-display text-2xl font-semibold">50</dt>
                <dd className="text-xs text-white/65">monitored breaks</dd>
              </div>
              <ForecastHorizon />
              <div>
                <dt className="font-display text-2xl font-semibold">2–10 days</dt>
                <dd className="text-xs text-white/65">default alert lead time</dd>
              </div>
            </dl>
          </div>
          <div className="lg:pt-6">
            <LiveSwells />
          </div>
        </div>

        <svg className="absolute bottom-0 left-0 h-40 w-[200%] wave-layer-slow" viewBox="0 0 2880 160" preserveAspectRatio="none" aria-hidden="true">
          <path d="M0 80c240 0 240-60 480-60s240 60 480 60 240-60 480-60 240 60 480 60 240-60 480-60 240 60 480 60v80H0z" fill="#0b4f66" opacity=".55" />
        </svg>
        <svg className="absolute bottom-0 left-0 h-28 w-[200%] wave-layer" viewBox="0 0 2880 120" preserveAspectRatio="none" aria-hidden="true">
          <path d="M0 60c180 0 180-40 360-40s180 40 360 40 180-40 360-40 180 40 360 40 180-40 360-40 180 40 360 40 180-40 360-40 180 40 360 40v60H0z" fill="#f6fafb" />
        </svg>
      </section>

      <section className="bg-background">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6">
          <div className="max-w-2xl">
            <p className="text-xs font-semibold tracking-[0.16em] text-ocean uppercase">How it works</p>
            <h2 className="mt-2 font-display text-4xl font-semibold text-ink">From offshore swell to boarding pass.</h2>
          </div>
          <ol className="mt-10 grid gap-5 md:grid-cols-2 lg:grid-cols-4">
            {STEPS.map(({ icon: Icon, title, body }, i) => (
              <li key={title} className="relative rounded-2xl border bg-card p-6 shadow-sm">
                <span className="absolute top-5 right-5 font-display text-4xl font-semibold text-secondary">{i + 1}</span>
                <div className="mb-4 inline-flex rounded-xl bg-secondary p-2.5 text-ocean">
                  <Icon className="size-5" />
                </div>
                <h3 className="font-semibold text-ink">{title}</h3>
                <p className="mt-2 text-sm text-muted-foreground">{body}</p>
              </li>
            ))}
          </ol>
          <Link href="/how-to-use" className="mt-6 inline-flex items-center gap-1.5 text-sm font-medium text-ocean hover:underline">
            Read the guide: how to use Swell Travel Agent <ArrowRight className="size-4" />
          </Link>
        </div>
      </section>

      <section className="bg-sand">
        <div className="mx-auto grid max-w-7xl gap-10 px-4 py-20 sm:px-6 lg:grid-cols-[1fr_1.4fr]">
          <div>
            <p className="text-xs font-semibold tracking-[0.16em] text-ocean uppercase">Built for surfers who travel</p>
            <h2 className="mt-2 font-display text-4xl font-semibold text-ink">The forecast does the planning.</h2>
            <p className="mt-4 text-muted-foreground">
              Every alert links to the full forecast, the surf window, the reasoning behind the score and the exact flight
              itinerary — with local times at both ends. Prices are never invented: in demo mode every fare is clearly
              labelled as mock data.
            </p>
            <Button asChild className="mt-6">
              <Link href="/register">
                Create your first search <ArrowRight />
              </Link>
            </Button>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            {FEATURES.map(({ icon: Icon, title, body }) => (
              <div key={title} className="rounded-2xl bg-card p-6 shadow-sm">
                <Icon className="size-5 text-coral" />
                <h3 className="mt-3 font-semibold text-ink">{title}</h3>
                <p className="mt-1.5 text-sm text-muted-foreground">{body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <SiteFooter dark />
    </div>
  );
}
