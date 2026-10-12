# Research confidence

Source: `backend/app/services/research/` (`values.py`, `evidence.py`, `assess.py`,
`views.py`, `cli.py`). Tables: `spot_research_sources`, `spot_research_claims`,
`spot_research_assessments` (migration `0004_confidence_map_fields`;
`spot_research_sources.retrieval` from `0005_research_retrieval`).
Methodology version: `research-v1.1` (stored on every assessment row; section 3 lists what
changed from `research-v1`).

## 1. What it is, and what it is not

**Research confidence** answers one question: *how well does the app understand this
break's ideal conditions?* It rates the published evidence behind the spot profile in
`data/surf_spots/spots.json` (preferred swell direction, swell size, swell period, wind,
tide, bottom shape, season and how the wave breaks) as **High / Moderate / Low**, per
variable and overall, and lists the gaps in plain language, for example *"Limited
information is available about this break's preferred tide conditions."*

It is **not**:

* **Forecast confidence.** Forecast confidence says how trustworthy an upcoming prediction
  is (ensemble spread, lead time, model agreement; see `SURF_SCORING.md`). Research
  confidence never looks at a forecast. The two are stored separately (research on
  `spot_research_assessments`, forecast on each prediction) and shown separately; they are
  never combined into one number. The prediction job may *read* the research class as one
  documented input to the local (breaking-height) confidence class, but the research class
  itself is never changed by it.
* **A probability.** The classes are a documented, rule-based classification of evidence.
  They are not calibrated and say nothing like "a 70 % chance the profile is right".
* **A completeness score.** Filling in more profile fields never raises it. A profile can
  have every field filled in and still be Low when the evidence behind it is thin.
* **A quality rating of the wave.**

## 2. Evidence

Evidence lives in `data/surf_spots/research_evidence.json` (format in section 8). Each
**source** is one page or publication that was actually consulted; each **claim** is what
that source says about one variable, with a short excerpt (≤ 300 characters) and, where
possible, a normalised value. Each source also records **how it was read**
(`retrieval`): `fetched` (the default) when the page or publication itself was read and the
excerpt is copied from it, `web_search` when only a search tool's text about the page was
available, so the excerpt is that summary and not the page's own words. Search-only
evidence is weaker (the summary may compress or misattribute what the page says) and is
capped in section 3. The loader validates the whole file and reports every
problem at once; the sync into the tables is idempotent (section 6).

### Source tiers

| Tier | `source_type` | Why |
|------|---------------|-----|
| A | `survey_data`, `peer_reviewed`, `official` | Measured, reviewed, or published by an accountable authority (government, harbour, marine park, event organiser). |
| B | `guidebook`, `encyclopedia`, `surf_forecast_site`, `magazine` | Edited publications with a reputation to keep, but usually unreferenced and often copying each other. |
| C | `community` | Forums, blogs and user-generated spot pages: useful leads, not confirmation. |

### Independence

Agreement between copies is not evidence. Sources are grouped into **independent
sources**: two sources with the same publisher (compared case- and space-insensitively),
or two pages on the same web host (ignoring a leading `www.` / `m.`), count as **one**.
A group's tier is the best tier among its sources. All counts in the rules below are
counts of independent sources.

### Agreement with the profile

Every claim is compared with the current profile **in code** when it has a structured
value (`values.py`):

| Variable | Value | Agrees when |
|----------|-------|-------------|
| `swell_direction` | `{"min", "max"}` degrees FROM, clockwise, may wrap through north; one bound may be omitted (`{"min": 320}`, "320° or more") | the window overlaps the profile's optimal window widened by 30° on each side; a one-bound value is compared as that single direction (the open side of a circular range is undefined) and is never exact |
| `wind` | `{"offshore_from_deg"}` | within 45° of the profile's offshore wind direction |
| `tide` | `{"tide"}` (`low`, `low_to_mid`, `mid`, `mid_to_high`, `high`, `all`) | same class, or adjacent on low → low_to_mid → mid → mid_to_high → high; `all` matches only `all`; not comparable when the profile has no tide preference |
| `swell_size` | `{"min_ft", "max_ft"}` breaking **face** height in feet (one bound may be null or omitted) | the range overlaps the profile's working range |
| `swell_period` | `{"min_s", "max_s"}` seconds (one bound may be null or omitted) | the range overlaps the profile's `min_swell_period_s` .. `ideal_swell_period_s` |
| `seasonality` | `{"months": [..]}` | at least one month in common with `best_months`; not comparable when the profile lists none |
| `bathymetry`, `local_behavior` | `null` (descriptive) | the researcher's flag; `null` counts as a supporting description |

The computed result is what the methodology uses. When the researcher's `agrees` flag
differs, both are kept in the assessment details (`per_variable.*.evidence`,
`flag_differences`) and nothing is added to the user-facing text. A claim without a
structured value uses the researcher's flag (`null` = descriptive / not comparable).

Sizes must be breaking face heights. Offshore significant wave height and Hawaiian-scale
numbers are not the same quantity; such a claim gets `value: null` unless the source
itself states face heights.

An **exact** match (needed for one rule below) is stricter: direction-window ends and the
offshore direction within 11.25° (the same 16-point compass name), the same tide class,
both range bounds within 0.5 ft / 0.5 s, the same set of months.

## 3. Per-variable rules (research-v1.1)

*Supporting* = the claim agrees (or, for `bathymetry` and `local_behavior`, describes
without a judgement). *Contradicting* = it disagrees.

1. **Base class from the supporting evidence**
   * **High**: ≥ 2 independent supporting sources including a tier-A one, or ≥ 3
     independent supporting sources of tier B or better.
   * **Moderate**: ≥ 2 independent supporting sources of any tier, or one tier-A source, or
     one tier-B source whose value matches the profile exactly.
   * **Low**: anything else (no evidence, a single tier-C source, a single tier-B source
     that only agrees within the tolerances).
2. **Conflicts**, comparing the best contradicting tier with the best supporting tier:
   * same tier: an *unresolved conflict*, the class drops one step (High → Moderate,
     Moderate → Low) and the gap "Sources disagree about …" is added;
   * contradicting tier stronger, or no support at all: **Low** (the best evidence does not
     support the profile), with a gap;
   * contradicting tier weaker: *outweighed*; no change of class, the summary says
     "1 disagrees".
3. **Bathymetry cap.** Bathymetry can be High only with `survey_data` or `peer_reviewed`
   support. Guides describing "a shallow reef" cap it at Moderate (gap: "No survey or
   peer-reviewed study of the bottom was found.").
4. **Recency at sand-bottom breaks.** For `bathymetry` and `local_behavior` at sand-bottom
   breaks the class drops one step unless at least one supporting source has a known
   `published_year` at most 15 years before the research date (the year of the newest
   `accessed_at`). Sandbars move; an old description of them is weak evidence today. An
   unknown year never counts as recent. Sand-bottom = break type `beach` or `rivermouth`,
   or a description that says "sand-bottom", "sand bottom", "sandbar", "sand bar" or
   "sand spit" (e.g. the Gold Coast points, Skeleton Bay). The age is measured against the
   research date, not the clock, so an assessment changes only when its evidence does.
5. **Search-summary cap (new in research-v1.1).** A variable whose supporting sources were
   *all* read only as search summaries (`retrieval` = `web_search`) is capped at
   **Moderate** (rule `cap:search_only`, gap "Sources on … were seen only as search
   summaries, not read in full."). It is applied last, after rules 1-4, so it changes only
   what would otherwise be High; one supporting source that was read directly lifts it.
   Whenever a variable's evidence is search-only (its supporting sources, or all its sources
   when none supports), its summary ends "(based on search summaries; source pages not read
   in full)", whatever its class. Consequence: overall High needs directly read evidence for
   swell direction, size and wind.

`research-v1` → `research-v1.1`: rule 5, `retrieval` in the evidence fingerprint, and the
format accepts one-bound values (above). Rules 1-4 and the overall rule are unchanged.
Because the methodology version changed, the next assessment run inserts a new row for every
spot and keeps the research-v1 rows as history (section 5).

Each variable stores (`spot_research_assessments.per_variable[<variable>]`): `class`,
`label`, `sources` (pages), `independent_sources`, `independent_supporting`,
`independent_contradicting`, `strongest_source_type` (of the supporting sources),
`agreement` (`supporting` / `contradicting` / `not_comparable` page counts),
`newest_year`, `search_only` (rule 5's condition), `summary` (one plain sentence, e.g.
"Supported by 3 independent sources, including a peer-reviewed study."), `gaps`, `rules`
(which rules fired, e.g. `["base:high", "conflict:same_tier"]`), `flag_differences` and
`evidence` (one entry per claim: source metadata including `retrieval`, without the
excerpt, the value, the researcher's flag, the computed and the used agreement).

A variable without evidence gets a fixed gap sentence:

| Variable (label) | Gap without evidence |
|------------------|----------------------|
| Swell direction | Limited information is available about which swell directions work best at this break. |
| Swell size | Limited information is available about the wave sizes this break works in. |
| Swell period | Limited information is available about the swell period this break needs. |
| Wind | Limited information is available about which winds are offshore at this break. |
| Tide | Limited information is available about this break's preferred tide conditions. |
| Bottom / bathymetry | Limited information is available about the shape of the bottom at this break. |
| Season | Limited information is available about this break's best season. |
| How the wave breaks | Limited information is available about how this wave breaks. |

### Why these rules

* **Two independent sources, one of them strong**, is the smallest amount of evidence that
  can survive one source being wrong or copied. Three edited sources from different
  publishers are accepted as an alternative because tier-A sources simply do not exist for
  most surf breaks; requiring them would make High impossible almost everywhere.
* **Independence by publisher and host** stops one site's many spot pages, or a page and
  its mirror, from looking like a consensus.
* **A single edited source is Moderate only when it states exactly the profile's value**:
  agreement within a 30° or 45° tolerance from one unreferenced page is too weak to be more
  than Low.
* **Conflicts lower confidence instead of being averaged away**: a disagreement between
  sources of equal standing is a real uncertainty, and contradiction by stronger evidence
  means the profile itself may be wrong.
* **Bathymetry needs measurement**: "shallow reef" in a guide is a description, not a
  depth; the nearshore model has no measured depths for any spot today
  (`SURF_SCORING.md`, physical profiles).
* **Recency only where the bottom moves**: a lava reef does not change in 15 years; a
  sandbar can change in one winter.

## 4. Overall rule

The overall class is **not** an average and never depends on how many fields are filled:

* **Low** if swell direction or wind is Low, or 4 or more variables are Low;
* **High** only if swell direction, swell size and wind are all High and none of tide,
  bathymetry and local behaviour is Low;
* **Moderate** otherwise.

Swell direction, size and wind decide whether a break works at all on a given day, so
they carry the class; tide, bottom and local behaviour can only hold it back. Swell period
and season are useful but rarely decisive, so they affect the overall class only through
the "4 or more Low" rule.

The overall summary is one plain sentence; the overall gaps are the most important
missing or conflicting items, at most 4: conflicts first, then gaps of Low variables, then
gaps of Moderate ones, each group in the order swell direction, wind, swell size, tide,
bathymetry, local behaviour, swell period, season.

A spot with no evidence is assessed too: **Low**, with every per-variable gap, so the UI
can say what is missing.

## 5. History

Assessments are never updated in place or deleted. `assess_spot` computes the assessment
and its `evidence_fingerprint` (sha256 of the canonical JSON of the methodology version,
every source (including its `retrieval`) and claim of the spot, the profile values compared
and the research year).
It inserts a new row (and flips the previous row's `is_current` to false in the same
transaction, before the insert, so the partial unique index "one current row per spot"
holds) only when the fingerprint or the methodology version changed; otherwise it returns
the current row. `research_updated_at` is the newest `accessed_at` of the evidence.
Earlier assessments therefore stay available to evaluate a methodology change.

Changing the rules means bumping `METHODOLOGY_VERSION` in `assess.py`; the next
assessment run then adds one new row per spot.

## 6. Running it

* `python -m app.cli seed` / `init-db`: after the spots, loads the evidence file (if
  present) and assesses every spot.
* `python -m app.cli research load [--path P]`: validate and load an evidence file, then
  assess every spot. Exit 2 with every problem listed when the file is invalid; nothing is
  written then.
* `python -m app.cli research assess [--slug S] [--force]`: re-assess from the stored
  evidence (`--force` inserts a new row even when nothing changed).
* `python -m app.cli research show SLUG`: per-variable classes, sources and gaps.

Sync semantics: sources are upserted by (spot, `key`); a source's claims are replaced
only when they changed; sources no longer in the file (including every source of a spot
that is no longer in it) are deleted with their claims. A **missing file** means "no
evidence file": nothing is loaded and stored evidence is left as it is. To remove all
evidence, ship a file whose `spots` object is empty. The researchers' per-spot `notes`
are validated but not stored (there is no column for them); they stay in the file.

API: `GET /api/spots` and every other `SpotSummary` carry `research_class` (one query for
all spots); `GET /api/spots/{slug}` carries `research` (`ResearchOut`: overall class,
summary, gaps, one row per variable with its label, class, summary, gaps and source
count, the sources without excerpts but with `retrieval` (`null` in research-v1
assessments), `methodology_version`, `assessed_at`, `research_updated_at`). The sources come from the snapshot in the assessment, so they are
exactly the ones it used.

## 7. Adding evidence

1. Research one spot at a time. For every source, re-open the page and copy the excerpt
   verbatim (≤ 300 characters; the loader rejects longer ones). Never write an excerpt from
   memory, never cite a page you did not open, and never use Surfline or Windy content.
   When the page itself could not be read and only a search tool's summary of it was
   available, say so with `"retrieval": "web_search"` (the excerpt is then the summary's
   text); such evidence is capped at Moderate (section 3, rule 5).
2. Pick the `source_type` honestly (section 2). Use the publisher's real name and the same
   spelling every time, so pages of one publisher count once.
3. Give a structured `value` only when the source states one: directions as degrees FROM
   (a named direction such as "W–NW" becomes `{"min": 270, "max": 315}`), sizes only as
   face heights in feet, periods in seconds. Otherwise use `null`.
4. Set `agrees` to your own judgement against the profile (`true` / `false` / `null`).
   The code recomputes it where a value exists; differences are kept for review.
5. Record gaps and conflicts you found in the spot's `notes`.
6. Run `python -m app.cli research load` and `research show SLUG`; fix every reported
   problem.

## 8. Evidence file format

`data/surf_spots/research_evidence.json`, UTF-8 JSON:

```json
{
  "version": 1,
  "generated_at": "2026-10-11T00:00:00Z",
  "method": "how the evidence was gathered and verified (free text)",
  "spots": {
    "<spot slug>": {
      "notes": "gaps and conflicts found by the researchers (free text)",
      "sources": [
        {
          "key": "wikipedia-supertubos",
          "title": "...",
          "url": "https://...",
          "publisher": "Wikipedia",
          "source_type": "encyclopedia",
          "published_year": 2023,
          "accessed_at": "2026-10-11",
          "retrieval": "fetched",
          "claims": [
            {
              "variable": "swell_direction",
              "claim": "one sentence in the researcher's words",
              "value": {"min": 270, "max": 320},
              "excerpt": "verbatim text from the page",
              "agrees": true
            }
          ]
        }
      ]
    }
  }
}
```

Validation (all problems are reported together; nothing is written when there is one):

* top level: exactly `version` (= 1), `generated_at` (ISO date-time), `method`
  (non-empty), `spots` (object keyed by an existing spot slug); spot entries: `notes`
  (optional string), `sources` (list);
* source: `key` (lower-case letters, digits, `.`, `_`, `-`; unique within the spot),
  `title`, `publisher` (non-empty), `url` (an `http(s)` URL, or `null` for a printed
  work), `source_type` (one of the eight types), `published_year` (a year or `null`),
  `accessed_at` (a date such as `2026-10-11`, not in the future), `claims` (non-empty),
  optional `notes`, optional `retrieval` (`fetched` (default) or `web_search`);
* claim: `variable` (one of the eight variables), `claim` and `excerpt` (non-empty,
  excerpt ≤ 300 characters), `value` (`null` or the variable's shape from section 2; for
  swell direction, size and period one of the two bounds may be left out, not both),
  `agrees` (`true` / `false` / `null`);
* unknown fields are rejected, so typos do not pass silently.

## 9. The committed evidence file (2026-10-11)

`data/surf_spots/research_evidence.json` holds the evidence assembled on 2026-10-11 for all
50 spots: 260 sources and 807 claims from domain-restricted web searches (one publisher per
query), every source and claim re-checked by an independent verifier with its own searches.
Direct page access was blocked by the research environment's network policy, so **every
source has `"retrieval": "web_search"`**: the excerpts are the search tool's text about the
pages, not quotations from the pages. Source types: 60 magazine, 60 official, 52 surf
forecast site, 39 community, 38 encyclopedia, 5 peer-reviewed, 4 guidebook, 2 survey data.
Sixteen claims state only one bound (e.g. "up to 30 ft", "320° or more"), which the format
accepts since research-v1.1.

Loaded with `research load` on a fresh database (2026-10-12, research-v1.1):

| Variable | High | Moderate | Low | `cap:search_only` fired |
|---|---:|---:|---:|---:|
| Swell direction | 0 | 43 | 7 | 19 |
| Swell size | 0 | 25 | 25 | 15 |
| Swell period | 0 | 12 | 38 | 3 |
| Wind | 0 | 32 | 18 | 10 |
| Tide | 0 | 16 | 34 | 6 |
| Bottom / bathymetry | 0 | 30 | 20 | 3 |
| Season | 0 | 29 | 21 | 12 |
| How the wave breaks | 0 | 36 | 14 | 20 |
| **Overall** | **0** | **18** | **32** | |

**No spot is High overall**, because rule 5 caps every variable at Moderate when all of its
support is search-only, and in this file all of it is. Without the cap (the research-v1
rules on the same evidence) Jaws, Sunset Beach and The Box would be High overall (and 19
spots High on swell direction). Examples: Jaws is Moderate (swell direction, size, wind,
bathymetry, season and local behaviour capped from High; gaps: no swell-period evidence,
and swell direction, wind and size seen only as search summaries); Pipeline is Moderate (no
swell-size claim, a single source for wind, tide, period and season, bathymetry capped as
unmeasured); Nazaré is Low (a single forecast-site source for swell direction, no evidence
for size, period, bathymetry or local behaviour, and no listed tide preference to compare
with).

