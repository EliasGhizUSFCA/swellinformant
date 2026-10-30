import Link from "next/link";
import type { ReactNode } from "react";

import { Card, CardContent } from "@/components/ui/card";
import { NO_FORECAST_COLOR, QUALITY_COLORS, QUALITY_COLORS_DARK, QUALITY_LABELS, QUALITY_OPTIONS, QUALITY_THRESHOLDS } from "@/lib/quality";
import { cn } from "@/lib/utils";
import type { QualityLabel } from "@/types/api";

/**
 * The "How to use" guide (/how-to-use): static, task-oriented help for every page of the app.
 *
 * Every statement here describes what the code does today, in the UI's own words: a label in
 * <Ui> is the exact text of a button, field, badge or heading on that page. When a label or a
 * behaviour changes, change it here too (tests/how-to-use.test.tsx checks the sections and the
 * table of contents; the quality classes and colours are read from lib/quality, never retyped).
 * Things that depend on the site's set-up (demo data, mock fares, the ocean layers, SMS, a
 * development inbox) are said to depend on it.
 */

export const HOW_TO_USE_SECTIONS = [
  { id: "what-it-does", title: "What the app does" },
  { id: "surf-map", title: "Read the surf map" },
  { id: "spot-page", title: "Read a spot page" },
  { id: "ratings", title: "Ratings and confidence" },
  { id: "forecast-updates", title: "How forecasts stay current" },
  { id: "account", title: "Create an account" },
  { id: "searches", title: "Save a search" },
  { id: "matching", title: "How matching works" },
  { id: "trips-flights", title: "Trips and flights" },
  { id: "alerts", title: "Alerts and emails" },
  { id: "demo-mode", title: "Demo mode and test sites" },
  { id: "status", title: "Status page" },
  { id: "faq", title: "Tips and questions" },
] as const;

export type HowToUseSectionId = (typeof HOW_TO_USE_SECTIONS)[number]["id"];

const titleOf = (id: HowToUseSectionId) => HOW_TO_USE_SECTIONS.find((s) => s.id === id)!.title;

const LINK = "font-medium text-ocean underline-offset-2 hover:underline rounded-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40";

/** A label exactly as the app shows it (a button, field, badge or heading). */
function Ui({ children }: { children: ReactNode }) {
  return <span className="rounded-md border bg-card px-1.5 py-px text-[0.9em] font-medium text-ink [box-decoration-break:clone]">{children}</span>;
}

/** A step of the search wizard, by its name in the wizard's step list. */
function Step({ children }: { children: ReactNode }) {
  return <strong className="font-semibold text-ink">{children}</strong>;
}

/** An app page, linked. */
function PageLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className={LINK}>
      {children}
    </Link>
  );
}

/** A link to another section of this guide. */
function SeeAlso({ id }: { id: HowToUseSectionId }) {
  return (
    <a href={`#${id}`} className={LINK}>
      {titleOf(id)}
    </a>
  );
}

/** A small example of what the app shows, set apart from the text. */
function Example({ label = "Example", children }: { label?: string; children: ReactNode }) {
  return (
    <figure className="my-4 rounded-xl border bg-secondary/40 px-4 py-3 text-sm">
      <figcaption className="mb-1 text-xs font-semibold tracking-[0.12em] text-ocean uppercase">{label}</figcaption>
      <div className="text-ink">{children}</div>
    </figure>
  );
}

function Section({ id, children }: { id: HowToUseSectionId; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-24" data-testid={`howto-section-${id}`}>
      <h2 id={`${id}-title`} className="font-display text-2xl font-semibold text-ink sm:text-3xl">
        {titleOf(id)}
      </h2>
      <div className="mt-3 space-y-3 text-[15px] leading-relaxed text-ink/85 sm:text-base">{children}</div>
    </section>
  );
}

function H3({ children }: { children: ReactNode }) {
  return <h3 className="pt-3 text-lg font-semibold text-ink">{children}</h3>;
}

function List({ ordered = false, children }: { ordered?: boolean; children: ReactNode }) {
  const Tag = ordered ? "ol" : "ul";
  return <Tag className={cn("space-y-1.5 pl-5", ordered ? "list-decimal" : "list-disc")}>{children}</Tag>;
}

/** The marker key of the dark map, drawn as the map's own legend draws it
 * (components/map/legend-drawer.tsx: the colours, the arrows, the label and the stale ring). */
function MarkerKey() {
  const classes = Object.keys(QUALITY_COLORS_DARK) as QualityLabel[];
  const colours = [{ label: "No usable forecast", color: NO_FORECAST_COLOR }, ...classes.map((q) => ({ label: QUALITY_LABELS[q], color: QUALITY_COLORS_DARK[q] }))];
  return (
    <div className="my-4 space-y-3 rounded-xl bg-[#151a21] px-4 py-3 text-xs text-white/90" data-testid="howto-marker-key">
      <ul className="flex flex-wrap gap-x-4 gap-y-2" aria-label="Spot marker colours on the dark map">
        {colours.map((m) => (
          <li key={m.label} className="flex items-center gap-1.5">
            <span className="size-2.5 shrink-0 rounded-full ring-1 ring-white/60" style={{ backgroundColor: m.color }} aria-hidden="true" />
            {m.label}
          </li>
        ))}
      </ul>
      <ul className="flex flex-wrap gap-x-5 gap-y-2 border-t border-white/10 pt-3" aria-label="Spot marker arrows and labels">
        <li className="flex items-center gap-2">
          <svg viewBox="-2 -6 22 12" className="h-3 w-6 shrink-0" aria-hidden="true">
            <path d="M17,0L10.5,-4.4L10.5,-1.4L0,-1.4L0,1.4L10.5,1.4L10.5,4.4Z" fill="#c4ec4a" stroke="#0b1620" strokeWidth={1} strokeLinejoin="round" />
          </svg>
          Swell
        </li>
        <li className="flex items-center gap-2">
          <svg viewBox="-2 -6 22 12" className="h-3 w-6 shrink-0" aria-hidden="true">
            <path d="M0,0L16,0M12,-3.2L16,0L12,3.2" fill="none" stroke="#0b1620" strokeWidth={3.4} strokeLinecap="round" strokeLinejoin="round" opacity={0.85} />
            <path d="M0,0L16,0M12,-3.2L16,0L12,3.2" fill="none" stroke="#fff" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Wind
        </li>
        <li className="flex items-center gap-2">
          <span className="shrink-0 rounded-[4px] bg-[#101418]/80 px-1 text-[10px] font-semibold text-white ring-1 ring-white/20 tabular-nums">4–6 ft</span>
          Surf height
        </li>
        <li className="flex items-center gap-2">
          <svg viewBox="-8 -8 16 16" className="size-4 shrink-0" aria-hidden="true">
            <circle r={6.5} fill="none" stroke="#fbbf24" strokeWidth={1.6} strokeDasharray="2.5 2" />
            <circle r={3.5} fill="#589bb7" stroke="#fff" strokeWidth={1} />
          </svg>
          Out of date
        </li>
      </ul>
    </div>
  );
}

/** The quality classes with their lower bounds on the x/10 scale and the wizard's own hints. */
function QualityTable() {
  const hints = new Map<string, string>(QUALITY_OPTIONS.map((o) => [o.value, o.hint]));
  return (
    <div className="my-4 overflow-x-auto rounded-xl border bg-card">
      <table className="w-full text-left text-sm" data-testid="howto-quality-table">
        <caption className="sr-only">Surf quality classes</caption>
        <thead className="bg-muted/60 text-xs text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Class
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Rating
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              What it means
            </th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {QUALITY_THRESHOLDS.map(([min, q]) => (
            <tr key={q}>
              <th scope="row" className="px-3 py-2 font-medium whitespace-nowrap text-ink">
                <span className="mr-2 inline-block size-2.5 rounded-full align-middle" style={{ backgroundColor: QUALITY_COLORS[q] }} aria-hidden="true" />
                {QUALITY_LABELS[q]}
              </th>
              <td className="px-3 py-2 whitespace-nowrap tabular-nums">{min > 0 ? `${(min / 10).toFixed(1)}/10 and up` : `below ${(QUALITY_THRESHOLDS.at(-2)![0] / 10).toFixed(1)}/10`}</td>
              <td className="px-3 py-2 text-muted-foreground">{hints.get(q) ?? "Below the Fair class."}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function TableOfContents({ className }: { className?: string }) {
  return (
    <nav aria-label="On this page" className={className} data-testid="howto-toc">
      <Card className="py-0">
        <CardContent className="p-4">
          <p className="mb-2 text-xs font-semibold tracking-[0.16em] text-ocean uppercase">On this page</p>
          <ol className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-sm lg:grid-cols-1">
            {HOW_TO_USE_SECTIONS.map((s, i) => (
              <li key={s.id}>
                <a
                  href={`#${s.id}`}
                  className="flex gap-1.5 rounded-lg px-1.5 py-1 text-muted-foreground outline-none hover:bg-muted hover:text-ink focus-visible:ring-[3px] focus-visible:ring-ring/40 sm:gap-2 sm:px-2 sm:py-1.5"
                >
                  <span className="w-5 shrink-0 text-right tabular-nums text-ocean/70" aria-hidden="true">
                    {i + 1}
                  </span>
                  {s.title}
                </a>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>
    </nav>
  );
}

export function HowToUseGuide() {
  return (
    <div className="grid gap-8 lg:grid-cols-[250px_minmax(0,1fr)] lg:gap-12">
      <TableOfContents className="lg:sticky lg:top-24 lg:max-h-[calc(100dvh-7rem)] lg:self-start lg:overflow-y-auto" />

      <div className="min-w-0 max-w-3xl space-y-14" data-testid="howto-content">
        <Section id="what-it-does">
          <p>
            Swell Travel Agent watches the surf forecast at 50 well-known surf spots and rates the surf at each one for every forecast time step.
            Save a search with the waves you want, where you fly from and your budget. When a swell lines up, the app looks for flights that
            land before the swell and leave after it, and alerts you.
          </p>
          <List>
            <li>
              <strong className="font-semibold text-ink">No account needed:</strong> the <PageLink href="/map">Surf map</PageLink>, the{" "}
              <PageLink href="/spots">Spots</PageLink> list and every spot page, this guide and the <PageLink href="/status">System status</PageLink> page.
            </li>
            <li>
              <strong className="font-semibold text-ink">With an account:</strong> <Ui>Dashboard</Ui>, <Ui>Searches</Ui>, <Ui>Alerts</Ui> and{" "}
              <Ui>Settings</Ui>.
            </li>
          </List>
          <H3>Quick start</H3>
          <List ordered>
            <li>
              Open the <PageLink href="/map">Surf map</PageLink> and select a spot to see its forecast.
            </li>
            <li>
              Select <Ui>Full forecast &amp; charts</Ui> for the spot&apos;s whole forecast.
            </li>
            <li>
              Select <Ui>Get started</Ui> to create an account, then <Ui>New search</Ui>.
            </li>
            <li>Check your Dashboard and your email for trips. The servers keep checking while the site is closed.</li>
          </List>
        </Section>

        <Section id="surf-map">
          <p>
            The <PageLink href="/map">Surf map</PageLink> shows every spot as a marker. Everything on it is for the time selected in the timeline at
            the bottom.
          </p>
          <H3>Markers</H3>
          <List>
            <li>
              The colour is our predicted surf quality at that break for the selected time. On the dark map a brighter blue is better surf. Open the{" "}
              <Ui>Legend</Ui> tab on the left edge for the full key.
            </li>
          </List>
          <MarkerKey />
          <List>
            <li>The lime arrow is the swell that drives the surf, pointing where it travels. The white arrow is the wind, pointing where it blows.</li>
            <li>
              The small label (for example <Ui>4–6 ft</Ui>) is our predicted surf height, as breaking wave faces. Labels appear when you zoom in to a
              region; arrows appear where markers have room.
            </li>
            <li>A dashed amber ring means the forecast at that spot is out of date.</li>
            <li>Hover over or tap a marker for the numbers.</li>
          </List>
          <H3>Time</H3>
          <List>
            <li>
              Drag along the timeline to change the time. <Ui>Play forecast</Ui> steps through it, <Ui>Now</Ui> goes back to the current time. On
              wider screens the arrows beside it move one step.
            </li>
            <li>The bubble shows the selected time in your own time zone.</li>
            <li>With a keyboard: arrow keys move one step, Page Up and Page Down one day, Home and End to either end.</li>
          </List>
          <H3>Ocean layers</H3>
          <p>
            Where this site has them switched on, the tabs at the top change the colour layer: <Ui>Wind Speed</Ui>, <Ui>Wave Height</Ui>,{" "}
            <Ui>Wave Energy</Ui> and <Ui>Wave Period</Ui>, and <Ui>Pressure</Ui> adds isobars. These are open-ocean model values, not surf at the
            beach. Without them the map says <Ui>Spot forecasts only</Ui> or <Ui>Ocean layers are unavailable right now.</Ui> and the spot markers
            work as usual.
          </p>
          <H3>Find a spot</H3>
          <List>
            <li>
              <Ui>Best spots now</Ui> lists the best spots at the selected time (<Ui>Best spots · 9am Sun PDT</Ui> for another time). Select one to
              open it.
            </li>
            <li>
              <Ui>Settings</Ui> (top right) has the units (<Ui>Wave height</Ui> in ft or m, <Ui>Wind</Ui> in km/h, mph, kn or m/s), the{" "}
              <Ui>Basemap</Ui> (<Ui>Dark</Ui> or <Ui>Streets</Ui>), <Ui>Animated flow</Ui>, <Ui>Wind on wave layers</Ui> and spot filters:{" "}
              <Ui>Region</Ui>, <Ui>Country</Ui>, <Ui>Break type</Ui>, <Ui>Min. breaking height</Ui>, <Ui>Min. quality</Ui> and{" "}
              <Ui>Swell event at this time</Ui>. It says how many spots match.
            </li>
            <li>
              The location button (bottom left, <Ui>Show my location</Ui>) moves the map to where you are, if your browser allows it.
            </li>
          </List>
          <H3>A selected spot</H3>
          <p>
            Selecting a spot opens its panel (a sheet at the bottom on phones): our prediction at the selected time next to the offshore model data,
            the spot research confidence, <Ui>Best surf window, next 7 days</Ui>, <Ui>Upcoming swell events</Ui> and, when you are signed in,{" "}
            <Ui>Your travel opportunities</Ui>. <Ui>Full forecast &amp; charts</Ui> opens the spot page, at the time you picked on the timeline.
          </p>
        </Section>

        <Section id="spot-page">
          <p>
            Every spot has its own page: open it from the map or from <PageLink href="/spots">Spots</PageLink>. Times on a spot page are the
            spot&apos;s local time. From the top:
          </p>
          <List>
            <li>
              <Ui>Best surf, next 7 days</Ui>: the best rated time of the coming week, with <Ui>Show in timeline</Ui>. Below it the rating (see{" "}
              <SeeAlso id="ratings" />
              ), our estimate of the breaking waves (<Ui>est. breaking faces</Ui>) with its <Ui>Likely range</Ui>, the <Ui>Offshore swell</Ui>{" "}
              (height @ period from direction) and the wind (speed, direction and how it blows at this break, such as{" "}
              <Ui>Moderate cross-onshore</Ui>). The small arrows point where the swell and the wind are going.
            </li>
            <li>
              <Ui>About this break</Ui>: how well the break itself is researched, shown apart from the forecast.
            </li>
            <li>
              <Ui>Surf forecast</Ui>: the timeline. Rows for <Ui>Surf quality</Ui>, <Ui>Surf</Ui> (our breaking-wave estimate), <Ui>Swell</Ui> (the
              offshore wave systems), <Ui>Wind</Ui> and <Ui>Tide</Ui> share one time axis. Tap or drag across it, use the day buttons, or the arrow
              keys, to pick a step: the summary above it shows that step in full. The day buttons show each day&apos;s best rating.
            </li>
            <li>
              Above the rows: <Ui>Offshore Hs (model)</Ui> adds the model&apos;s offshore wave height to the Surf row, <Ui>ft</Ui> / <Ui>m</Ui>{" "}
              switches heights and <Ui>km/h</Ui>, <Ui>mph</Ui>, <Ui>kn</Ui>, <Ui>m/s</Ui> the wind. This browser remembers your height unit for the
              whole site, the map included.
            </li>
            <li>
              Tide: the height and whether it is rising or falling. When the tide counts in the rating, the reading says so, for example{" "}
              <Ui>Favorable tide for this break (in the rating)</Ui>.
            </li>
            <li>
              Further down: <Ui>Our prediction and the model data</Ui> for the selected step, <Ui>Compare forecast models</Ui>,{" "}
              <Ui>Upcoming swell events</Ui>, <Ui>Your travel opportunities here</Ui> (signed in), <Ui>Spot characteristics</Ui> (best swell,
              offshore wind, period, working size, tide, best months), <Ui>Getting there</Ui> (airports and transfer times) and{" "}
              <Ui>About this forecast</Ui> (models, model runs and method).
            </li>
          </List>
        </Section>

        <Section id="ratings">
          <p>Every forecast time step gets a surf quality rating for that break, written as one line:</p>
          <Example>
            <span className="font-semibold">Good Surf — 6.1/10</span> — Well-aligned swell and light offshore wind.
          </Example>
          <p>
            That is the class, the rating out of 10 and a short reason built from the conditions that help or hold back the rating. The rating is our
            0–100 score divided by ten. It combines swell direction, period, size, wind, tide and consistency for this break. It ranks conditions; it
            is not a probability or a measurement.
          </p>
          <QualityTable />
          <H3>Why this rating?</H3>
          <p>
            <Ui>Why this rating?</Ui> opens the breakdown: what <Ui>Helps the rating</Ui> and what <Ui>Holds it back</Ui>, each factor marked{" "}
            <Ui>Favorable</Ui>, <Ui>Workable</Ui> or <Ui>Limiting</Ui>, anything that held the score back, and the 0–100 score itself.
          </p>
          <H3>Waves and wind</H3>
          <List>
            <li>
              Breaking waves are our estimate of the wave faces (trough to crest) where the waves break, worked out from the offshore swell. It is a
              model estimate, not a measurement. The <Ui>Likely range</Ui> is meant to hold the real height about 8 times in 10 but is marked{" "}
              <Ui>unverified</Ui>: treat it as a guide.
            </li>
            <li>
              Offshore wind blows from the land towards the sea and grooms the waves; onshore wind blows in from the sea and makes them choppy.
              Cross-shore is in between. Each break has its own offshore direction. The labels run <Ui>Glassy</Ui>, <Ui>Offshore</Ui>,{" "}
              <Ui>Cross-offshore</Ui>, <Ui>Cross-shore</Ui>, <Ui>Cross-onshore</Ui>, <Ui>Onshore</Ui>, often with a strength such as light or
              moderate.
            </li>
            <li>
              Readings say where the swell and the wind come <em>from</em> (<Ui>from NNW</Ui>); arrows point where they are going.
            </li>
          </List>
          <H3>Two kinds of confidence</H3>
          <p>The app shows two separate assessments and never combines them into one number.</p>
          <List>
            <li>
              <Ui>Forecast confidence</Ui> (<Ui>High</Ui>, <Ui>Moderate</Ui> or <Ui>Low</Ui>), for each forecast time: how much the forecast may still
              change, based on how far ahead it is, whether models agree and data quality. <Ui>Confidence details</Ui> lists the reasons.
            </li>
            <li>
              <Ui>Spot research confidence</Ui>, for each break: how well its ideal conditions (swell direction, size, period, wind, tide, bathymetry
              and season) are documented by credible sources. <Ui>Research details</Ui> lists the gaps and the sources. A break not checked yet reads{" "}
              <Ui>Not assessed yet</Ui>.
            </li>
          </List>
          <p>
            Neither is a probability. A forecast 9 days out at a well-researched break is still a forecast 9 days out; a fresh forecast at a poorly
            documented break is still poorly documented.
          </p>
        </Section>

        <Section id="forecast-updates">
          <p>
            In the background, the site checks the weather models for new runs and refreshes the spot forecasts when a new wave or wind run comes
            out, whether or not anyone has the site open. The ratings, swell events and trip matches are worked out again with it.
          </p>
          <p>Open pages pick up the new forecast by themselves, without a reload, while their tab is visible. This label says what you are looking at:</p>
          <Example label="On the map and spot pages">
            <span className="inline-flex items-center gap-1.5 rounded-full border bg-card px-2.5 py-0.5 text-xs text-muted-foreground">
              Forecast updated <span className="font-medium text-ink">14:32 PDT</span> · 5 min ago
            </span>
          </Example>
          <List>
            <li>On the map it is when the forecast was published, in your time zone; on a spot page, when that spot was last updated, in its time zone.</li>
            <li>When a newer forecast arrives the label changes with the data and is highlighted for a moment; screen readers hear “New forecast loaded”.</li>
            <li>
              A spot whose forecast was not updated says <Ui>Forecast last updated 7 hours ago. Not a current forecast.</Ui> and a{" "}
              <Ui>Forecast out of date</Ui> notice says why. While the model run itself is out of date, forecast confidence is capped at Moderate, and
              new alerts can be paused until fresh data arrives.
            </li>
          </List>
        </Section>

        <Section id="account">
          <List ordered>
            <li>
              Select <Ui>Get started</Ui> (top right). Fill in <Ui>Full name</Ui>, <Ui>Email</Ui>, <Ui>Password</Ui> (at least 10 characters, with
              letters and a number or symbol) and <Ui>Confirm password</Ui>, then <Ui>Create account</Ui>.
            </li>
            <li>
              You land on your Dashboard. Open the link in the verification email we send you. Until then the dashboard shows{" "}
              <Ui>Verify your email to get alerts</Ui> with <Ui>Resend link</Ui>: trips still show up, and email alerts start once your email is
              verified.
            </li>
            <li>
              In <PageLink href="/settings">Settings</PageLink> (in the account menu at the top right; in the menu on phones): your profile (home airport, height units,
              currency and more, used to pre-fill new searches), <Ui>Saved departure airports</Ui>, <Ui>Alerts</Ui>, <Ui>SMS alerts</Ui>,{" "}
              <Ui>Change password</Ui> and <Ui>Delete account</Ui>.
            </li>
          </List>
          <p>
            Forgot your password? Use <Ui>Forgot password?</Ui> on the <PageLink href="/login">Sign in</PageLink> page.
          </p>
        </Section>

        <Section id="searches">
          <p>
            A search says what waves you want, where and when you can go, and what you will spend. Open <PageLink href="/searches">Searches</PageLink>{" "}
            and select <Ui>New search</Ui> (also in the header). Six steps; you can change everything later.
          </p>
          <List ordered>
            <li>
              <Step>Surf preferences</Step>: <Ui>Desired wave height</Ui> (<Ui>Minimum</Ui>, <Ui>Maximum</Ui>, <Ui>Preferred (optional)</Ui>) as breaking
              faces in ft or m, and <Ui>Minimum surf quality</Ui> (Fair to Exceptional). Under <Ui>Wave characteristics (optional)</Ui>: minimum swell
              period, <Ui>Wind</Ui> (<Ui>Any wind (score decides)</Ui>, <Ui>No onshore wind</Ui> or <Ui>Offshore, cross-offshore or glassy</Ui>),
              maximum wind speed, <Ui>Minimum surf window</Ui> (daylight hours that must meet all your criteria), swell direction, consistency and
              break types.
            </li>
            <li>
              <Step>Destinations</Step>: <Ui>All 50 spots</Ui>, <Ui>Regions or countries</Ui> or <Ui>Specific spots</Ui>, and optionally a{" "}
              <Ui>Maximum airport-to-spot transfer</Ui>.
            </li>
            <li>
              <Step>Travel availability</Step>: <Ui>Flexible — keep watching</Ui> with a <Ui>Monitoring horizon</Ui>, or <Ui>Fixed dates</Ui> (
              <Ui>Earliest day I can leave home</Ui>, <Ui>Latest day I must be back</Ui>). The travel buffers <Ui>Arrive before the swell</Ui> and{" "}
              <Ui>Leave after the swell</Ui> (days), <Ui>Minimum days at destination</Ui> and <Ui>Maximum trip length</Ui>. Then the{" "}
              <Ui>Alert lead time</Ui>: <Ui>Earliest (days ahead)</Ui> and <Ui>Latest (days ahead)</Ui> a swell may start for this search to alert you.
            </li>
            <li>
              <Step>Flight budget</Step>: <Ui>Departure airports</Ui> (up to 5), <Ui>Also consider airports within</Ui>,{" "}
              <Ui>Max round-trip fare per traveller</Ui> and <Ui>Currency</Ui>, <Ui>Travellers</Ui>, <Ui>Cabin</Ui>,{" "}
              <Ui>Max flight time (each way)</Ui>, <Ui>Maximum layovers (each way)</Ui>, <Ui>Preferred airlines (optional)</Ui>, and what to
              prioritise when several trips qualify: <Ui>Balanced</Ui>, <Ui>Best waves</Ui>, <Ui>Cheapest</Ui> or <Ui>Shortest</Ui>.
            </li>
            <li>
              <Step>Notifications</Step>: <Ui>Email</Ui>, <Ui>SMS</Ui>, <Ui>Email + SMS</Ui> or <Ui>Dashboard only</Ui>; <Ui>Send updates</Ui> and{" "}
              <Ui>Surf-only alerts</Ui> (also tell me when no flight fits my budget). SMS needs a verified phone number in Settings.
            </li>
            <li>
              <Step>Review &amp; activate</Step>: name the search, check the summary (<Ui>Edit</Ui> goes back to a step), then <Ui>Activate search</Ui>.
            </li>
          </List>
          <Example label="Travel buffers">
            With the default buffers (arrive 2 days early, leave 1 day after), a swell on Aug 10–13 means landing by Aug 8 and flying home on Aug 14.
            The app checks each itinerary&apos;s real arrival time, the time zones and the airport transfer.
          </Example>
          <p>
            Forecasts get less certain the further ahead they look: a swell a week or more out often shifts in size and timing, while 2–5 days out is
            usually firmer. Pick your alert lead time with that in mind.
          </p>
          <H3>Manage your searches</H3>
          <p>
            On <PageLink href="/searches">Searches</PageLink> each search shows <Ui>Active</Ui> or <Ui>Paused</Ui>, its matches (with flights,
            surf-only, checking flights) and when it was last checked. <Ui>Pause</Ui> stops new alerts; <Ui>Resume</Ui> starts them again. The{" "}
            <Ui>…</Ui> menu has <Ui>Edit</Ui>, <Ui>Matching history</Ui>, <Ui>Duplicate</Ui> (the copy starts paused) and <Ui>Delete</Ui>.
          </p>
        </Section>

        <Section id="matching">
          <p>
            After every new forecast, and when you save or resume a search, the servers check each active search. You do not need to keep the site
            open.
          </p>
          <List ordered>
            <li>The forecast is scanned for swell events: runs of daylight hours with rideable surf at each spot.</li>
            <li>
              A new match needs the swell to start inside the search&apos;s <Ui>Alert lead time</Ui>. A match found earlier keeps updating until the
              swell passes.
            </li>
            <li>
              The swell&apos;s daylight hours are checked again against your own criteria: quality, size, period, wind, direction and consistency.
              The best run of hours at least as long as your <Ui>Minimum surf window</Ui> becomes the surf window. Night hours never count.
            </li>
            <li>A flexible search accepts any window within its horizon; a fixed-date search needs the window inside your dates.</li>
            <li>
              The spot&apos;s primary airport is preferred, then the shortest transfer within your limit. With no usable airport the match is
              surf-only.
            </li>
          </List>
          <p>
            Each match then shows a status: <Ui>Flights found</Ui>, <Ui>Surf only</Ui>, <Ui>Checking flights</Ui>, <Ui>Expired</Ui> (the forecast no
            longer fits your search, or the swell has passed) or <Ui>Dismissed</Ui>.
          </p>
        </Section>

        <Section id="trips-flights">
          <p>
            The <PageLink href="/dashboard">Dashboard</PageLink> lists <Ui>Your surf trips</Ui>: first the <Ui>Short notice</Ui> trips (swells
            starting soon, the trips to book first), then <Ui>Further ahead (forecasts can still shift)</Ui>. It also shows the top surf forecasts,
            your active searches and recent alerts, and updates with every new forecast.
          </p>
          <H3>A trip</H3>
          <p>
            A trip page shows the swell (<Ui>Breaking waves at peak (our estimate)</Ui>, <Ui>Best surf window (local time at the break)</Ui>,{" "}
            <Ui>Swell (offshore)</Ui>, <Ui>Wind at peak</Ui>), its forecast confidence, the travel dates and the flight options. Each offer shows the
            price per traveller and in total, the airline and <Ui>Operated by</Ui>, local arrival and departure times, the <Ui>Itinerary</Ui> and when
            the price was quoted or re-checked.
          </p>
          <H3>When fares are searched</H3>
          <p>Flights are searched only when there is a reason:</p>
          <List>
            <li>a swell starts to fit one of your searches;</li>
            <li>the trip changes: the travel dates move or another airport is used;</li>
            <li>an expired match qualifies again;</li>
            <li>
              you select <Ui>Re-check flights</Ui> on the trip;
            </li>
            <li>right before an alert, if the fare has expired, sold out or gone over your budget.</li>
          </List>
          <p>An unchanged forecast never starts a new flight search.</p>
          <H3>Prices are quotes</H3>
          <List>
            <li>
              Fares are quotes and can change. An old quote is marked <Ui>Quote expired — re-check flights</Ui> and is no longer offered for booking.
            </li>
            <li>
              Where the flight provider gives one, a button opens its booking page. <Ui>Google Flights</Ui> and <Ui>Skyscanner</Ui> links are route
              searches for the same route and dates: fares there may differ.
            </li>
            <li>
              When no flight fits, the trip says <Ui>Surf matches — no eligible flight yet</Ui> with the reason, and the app keeps checking.{" "}
              <Ui>Dismiss</Ui> removes a trip you do not want.
            </li>
          </List>
        </Section>

        <Section id="alerts">
          <p>
            You get one alert per trip, by the channel your search uses. Follow-ups come only with <Ui>Send updates</Ui> on: when quality improves,
            the fare drops meaningfully, or dates shift. Email alerts start once your email is verified.
          </p>
          <H3>What an alert email contains</H3>
          <List>
            <li>the spot and country, the swell dates in the spot&apos;s local time and how far ahead they start;</li>
            <li>the predicted quality (x/10), our breaking-wave estimate, the main offshore waves and the wind;</li>
            <li>forecast confidence and, where assessed, research confidence, on separate rows;</li>
            <li>suggested travel dates and travel buffers;</li>
            <li>
              with a fare: the round-trip fare, the flight dates with local times, the airports and ground transfer, the airline and{" "}
              <Ui>Operated by</Ui>, the flight time and when the price was checked;
            </li>
            <li>booking and route-search links, and links to your dashboard, to manage alerts and to unsubscribe.</li>
          </List>
          <H3>Your alert settings</H3>
          <List>
            <li>
              <PageLink href="/settings">Settings</PageLink> → <Ui>Alerts</Ui>: <Ui>Pause all notifications</Ui> (searches keep running; nothing is
              sent while paused), <Ui>Email alerts</Ui> and <Ui>Maximum alerts per day</Ui> (1, 3, 5, 10 or 20).
            </li>
            <li>
              <Ui>SMS alerts</Ui>: verify a phone number with a code; reply STOP to any message to opt out. Only where this site has SMS set up.
            </li>
            <li>
              Every alert email has an <Ui>Unsubscribe from email alerts</Ui> link. Unsubscribing stops email alerts; your searches keep running and
              your trips stay on the dashboard.
            </li>
          </List>
          <H3>Alert history</H3>
          <p>
            <PageLink href="/notifications">Alerts</PageLink> lists every alert, how it was delivered and how the trip looks now (
            <Ui>Fare at alert</Ui>, <Ui>Trip now</Ui>). Only <Ui>Delivered</Ui> means the email or SMS provider confirmed delivery; <Ui>Accepted</Ui>{" "}
            means it took the message.
          </p>
        </Section>

        <Section id="demo-mode">
          <p>Some sites run on test data. A yellow bar at the top of the app&apos;s pages (the map, spots, dashboard and the rest) says so:</p>
          <Example label="Demo mode bar">
            <span className="block rounded-md bg-amber-100 px-3 py-1.5 text-center text-xs font-medium text-amber-900">
              Demo mode — forecasts are synthetic · flight fares are mock data. Nothing here is a real booking.
            </span>
          </Example>
          <List>
            <li>
              Synthetic forecasts are marked <Ui>Demo data</Ui>, mock flights <Ui>Mock fares</Ui>, and an alert email with a mock fare says{" "}
              <Ui>MOCK FARE — not a real price</Ui>. A demo tide reads <Ui>demo tide (synthetic)</Ui>.
            </li>
            <li>
              A development site may capture emails instead of sending them: the dashboard&apos;s email verification notice then says{" "}
              <Ui>Development mode: emails are captured, not sent.</Ui> and a message status reads <Ui>Captured (dev inbox)</Ui>.
            </li>
            <li>Without the yellow bar, forecasts come from real weather models and fares are quotes from the flight provider.</li>
          </List>
        </Section>

        <Section id="status">
          <p>
            <PageLink href="/status">System status</PageLink> (in the footer) shows how fresh the data is: an overall <Ui>Data health</Ui> line (
            <Ui>All data checks pass</Ui>, <Ui>Degraded</Ui> or <Ui>Failing</Ui>, with the reasons), the <Ui>Forecast sync</Ui>, each forecast
            source with its latest model run, <Ui>Background jobs</Ui>, and which forecast, flight, email and SMS providers this site uses.
          </p>
        </Section>

        <Section id="faq">
          <dl className="space-y-5">
            <div>
              <dt className="font-semibold text-ink">Why does the surf height differ from the Wave Height layer?</dt>
              <dd className="mt-1">
                The layer is the height of the open sea offshore. Waves change a lot as they reach shallow water, so the surf height is our separate
                estimate of the breaking faces at the break.
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-ink">Is 7.5/10 a 75 % chance of good surf?</dt>
              <dd className="mt-1">No. The rating ranks conditions; it is not a probability. Neither confidence is a probability either.</dd>
            </div>
            <div>
              <dt className="font-semibold text-ink">Which time zone are times in?</dt>
              <dd className="mt-1">
                The map&apos;s timeline uses your own time zone. Spot pages, trips and alert emails use the spot&apos;s local time.
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-ink">Do I need to keep the site open?</dt>
              <dd className="mt-1">No. Your searches are checked on our servers against every new forecast, and alerts are sent from there.</dd>
            </div>
            <div>
              <dt className="font-semibold text-ink">How far ahead can I trust a forecast?</dt>
              <dd className="mt-1">
                Less the further ahead it looks: a week or more out often shifts, 2–5 days out is usually firmer. Check the forecast confidence next
                to each rating.
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-ink">Feet or metres?</dt>
              <dd className="mt-1">
                Use <Ui>ft</Ui> / <Ui>m</Ui> above a spot&apos;s timeline or <Ui>Wave height</Ui> in the map&apos;s Settings. This browser remembers it
                for every page; without a choice, the <Ui>Height units</Ui> in your account settings apply.
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-ink">Why did I not get an alert?</dt>
              <dd className="mt-1">
                Check that your email is verified, the search is <Ui>Active</Ui>, its channel is not <Ui>Dashboard only</Ui>, the swell starts inside
                its <Ui>Alert lead time</Ui>, and that <Ui>Pause all notifications</Ui> is off and the <Ui>Maximum alerts per day</Ui> was not reached.
                Your trips still show on the dashboard.
              </dd>
            </div>
          </dl>
        </Section>
      </div>
    </div>
  );
}
