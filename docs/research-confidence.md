# Research confidence

Source: `backend/app/services/research/` (`values.py`, `evidence.py`, `assess.py`,
`views.py`, `cli.py`). Tables: `spot_research_sources`, `spot_research_claims`,
`spot_research_assessments` (migration `0004_confidence_map_fields`;
`spot_research_sources.retrieval` from `0005_research_retrieval`).
Methodology version: `research-v1.2` (stored on every assessment row; section 3 lists what
changed from `research-v1` and `research-v1.1`). Profile decisions built on it: section 10
and `data/surf_spots/profile_decisions.json` (ADR-018).

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
| A | `survey_data`, `peer_reviewed`, `official` | Measured, reviewed, or published by an accountable authority with a mandate for the coast (see below). |
| B | `guidebook`, `encyclopedia`, `surf_forecast_site`, `magazine` | Edited publications with a reputation to keep, but usually unreferenced and often copying each other. |
| C | `community` | Forums, blogs and user-generated spot pages: useful leads, not confirmation. |

Tier decisions applied in the evidence (written down in research-v1.2; the second research
pass already applied them to the sources it read):

* **`official`** means a government agency with a coastal or marine mandate (a city council
  that manages its beaches, a sand-bypassing authority, a marine park), a national surfing
  reserve, or a government geological or hydrographic survey (e.g. the U.S. Geological
  Survey, Geological Survey Ireland). Measured survey products themselves (bathymetry
  tiles, multibeam surveys) are `survey_data`.
* **Tourism boards are `magazine` (tier B)**, even when they are government bodies: their
  mandate is promotion, not the coast (e.g. Visit Half Moon Bay, Tourism Fiji, El Salvador
  Travel, Visit Costa Rica). In research-v1.2 the seven first-pass tourism sources that
  were still `official` (GoHawaii.com ×3, The Margaret River Region ×3, Visit Mexico; all
  `web_search`) were reclassified to `magazine`; each says so in its `notes`.
* **World Surf League editorial pages are `magazine` (tier B)**: event previews, "Break
  Breakdown" and "Essentials" articles are journalism by the event organiser, not an
  accountable statement about the coast.

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
| `swell_size` | `{"min_ft", "max_ft"}` breaking **face** height in feet (one bound may be null or omitted) | the range overlaps the profile's working range **and** (research-v1.2) no stated bound is beyond 25 %: a stated maximum more than 25 % above the profile's maximum, or a stated minimum more than 25 % below the profile's minimum, contradicts that bound; a one-bound value is compared only on the bound it states; a narrower range (a maximum below, a minimum above the profile's) does not contradict |
| `swell_period` | `{"min_s", "max_s"}` seconds (one bound may be null or omitted) | the range overlaps the profile's `min_swell_period_s` .. `ideal_swell_period_s` |
| `seasonality` | `{"months": [..]}` | at least one month in common with `best_months`; not comparable when the profile lists none |
| `bathymetry`, `local_behavior` | `null` (descriptive) | the researcher's flag; `null` counts as a supporting description |

The computed result is what the methodology uses. When the researcher's `agrees` flag
differs, both are kept in the assessment details (`per_variable.*.evidence`,
`flag_differences`) and nothing is added to the user-facing text. A claim without a
structured value uses the researcher's flag (`null` = descriptive / not comparable).

Sizes must be breaking face heights. Offshore significant wave height and Hawaiian-scale
numbers are not the same quantity; such a claim gets `value: null` unless the source
itself states face heights. **Record waves are not working ranges**: "the biggest wave
ridden here was 80 ft" or the height of a named storm's waves says nothing about the sizes
at which the break works, so such a claim gets `value: null` (research-v1.2; the one such
value in the file, Mullaghmore's "about 60 ft" in two named storms, was removed). Likewise a
claim whose own text says it cannot be compared with the profile (e.g. "scale not stated,
probably swell height") carries no value: four such values were removed in research-v1.2,
each noted on its source.

An **exact** match (needed for one rule below) is stricter: direction-window ends and the
offshore direction within 11.25° (the same 16-point compass name), the same tide class,
both range bounds within 0.5 ft / 0.5 s, the same set of months.

## 3. Per-variable rules (research-v1.2)

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

`research-v1.1` → `research-v1.2`: swell-size values are compared bound by bound (the 25 %
tolerance in section 2; `values.SIZE_BOUND_TOLERANCE`), so a source that says a break holds
30 ft no longer "agrees" with a 4-12 ft working range just because the ranges overlap. The
tier decisions and the record-height rule above are written down (they change no code).
Rules 1-5, the search-only cap and the overall rule are unchanged; the version bump again
inserts one new assessment row per spot.

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
  depth. Measured values exist for four US spots only (NOAA BlueTopo, section 9): the
  survey depths at the forecast points of Mavericks, Ocean Beach and Rincon and Trestles'
  shore normal feed the nearshore model (Trestles' forecast point lies outside any survey);
  no spot has a measured controlling depth (`SURF_SCORING.md`, physical profiles).
* **Each size bound counts** (research-v1.2): overlap alone let "holds 30 ft" agree with a
  12 ft maximum; a 25 % tolerance on each stated bound keeps the usual rounding of face
  heights ("3-4 ft", "double overhead") from counting as disagreement.
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
* `python -m app.cli research lint [--decisions P] [--max-gap DEG]`: lists spots whose
  offshore wind is more than 60° from straight offshore (the shore normal the model uses
  + 180°) without a recorded reason, profile fields with contradicting evidence but no
  decision record, kept or disputed records the rule no longer gives, and problems in
  `profile_decisions.json` (exit 0 when nothing is listed, 1 with issues, 2 for an invalid
  file). Section 10.

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
   face heights in feet and only as a working range (never a record wave or a named
   storm's waves), periods in seconds. Otherwise use `null`. A claim you judge not
   comparable with the profile gets `null`, not a value with `agrees: null`.
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

## 9. The committed evidence file

`data/surf_spots/research_evidence.json` holds two research passes and the measured
bathymetry:

* **First pass (2026-10-11).** 259 sources and 805 claims from domain-restricted web
  searches while direct page access was blocked, all `retrieval: web_search` (ADR-016).
* **Second pass (2026-10-12).** Every source that could be read was re-read: 449 sources,
  384 read directly (`fetched`; excerpts confirmed verbatim on the page, every claim judged
  by an independent verifier: supported, correctly encoded, correct agreement), 1,711
  claims. The 65 sources behind bot protection are kept unchanged from the first pass as
  `web_search`. Publication years come from page metadata or visible dates (the
  last-modified year for continuously edited pages), `null` when the page has none.
  Assessed with research-v1.1: 34 spots Moderate, 16 Low, none High.
* **Research-v1.2 corrections (2026-10-13)**, each noted on its source: seven first-pass
  tourism sources `official` → `magazine` (section 2), values removed from four claims
  whose own text says they cannot be compared (two "scale not stated, probably swell
  height" sizes, a year-round "best season", a wind-sea hindcast direction at Uluwatu) and
  one record height (Mullaghmore).
* **NOAA BlueTopo bathymetry (2026-10-13).** 19 tiles for Mavericks, Ocean Beach, Trestles
  and Rincon, each a `survey_data` source read directly (the GeoTIFF and its raster
  attribute table, checksums verified against the BlueTopo tile scheme) with a bathymetry
  claim stating the depths (and, at Trestles' forecast point, that the cells are NBS
  Generalization, not survey depths); the excerpt is verbatim on the BlueTopo
  specifications page and in each tile's ImageDescription tag
  (`scripts/bathymetry_profiles.py`, `SURF_SCORING.md` physical profiles).

Totals now: **468 sources, 403 read directly, 65 search summaries, 1,730 claims**. Source
types: 176 magazine, 147 community, 55 surf forecast site, 41 encyclopedia, 20 survey data,
12 official, 10 peer-reviewed, 7 guidebook.

Loaded with `research load` on a fresh database (2026-10-13, research-v1.2, with the profile
changes of section 10); the research-v1.1 column is the second pass before this work:

| Variable | High | Moderate | Low | research-v1.1 (H / M / L) |
|---|---:|---:|---:|---|
| Swell direction | 25 | 23 | 2 | 24 / 23 / 3 |
| Swell size | 4 | 23 | 23 | 11 / 30 / 9 |
| Swell period | 4 | 16 | 30 | 4 / 16 / 30 |
| Wind | 11 | 26 | 13 | 11 / 26 / 13 |
| Tide | 4 | 14 | 32 | 4 / 12 / 34 |
| Bottom / bathymetry | 11 | 36 | 3 | 9 / 38 / 3 |
| Season | 24 | 23 | 3 | 24 / 23 / 3 |
| How the wave breaks | 38 | 10 | 2 | 38 / 10 / 2 |
| **Overall** | **0** | **35** | **15** | 0 / 34 / 16 |

What moved, step by step (each measured with the assessment code on the files):

1. the size bound rule alone changed 16 swell-size classes (Sunset Beach, Cloudbreak,
   Jeffreys Bay and Supertubos High → Low, Jaws High → Moderate, 11 spots Moderate → Low):
   size evidence now contradicts the profile at 25 spots instead of 1, and a conflict rule
   fires at 19 instead of 0;
2. the evidence corrections lifted Uluwatu's swell direction from Low to High (the
   wind-sea hindcast no longer counts against the groundswell window), and the tourism
   reclassification lowered Waimea Bay's and Puerto Escondido's swell size from High to
   Moderate (their tier-A support had been a tourism board);
3. BlueTopo made bathymetry High at Trestles and Rincon;
4. the profile changes made Ocean Beach's and Punta Roca's tide and Cloudbreak's size Low →
   Moderate, which lifted Ocean Beach from Low to Moderate overall (the only change of an
   overall class). Snapper Rocks' tide stays Low: after the change to `all`, a search
   summary of the same tier (Surfer Today, low to mid) still contradicts.

Research basis for local confidence (`forecast-confidence.md` §3): High 8 → 3, Moderate
30 → 23, Low 12 → 24 spots, almost entirely through the size rule.

The search-only cap (rule 5) fires for no variable: every variable with supporting evidence
has at least one directly read supporting source. **No spot is High overall**: only Rincon
and Kirra have swell direction, size and wind all High, and both have a Low tide (an
unresolved same-tier conflict about the tide).

## 10. Profile decisions and `research lint` (ADR-018)

The assessment says where the evidence contradicts a profile; `profile_decisions.json`
records what was done about it, one record per spot and profile field (offshore wind
direction, optimal swell window, working size range, tide preference, best months) whose
current assessment has a contradicting source:

```json
{"slug": "chicama", "field": "offshore_wind_direction", "old_value": 150.0,
 "new_value": null, "decision": "disputed", "alternative": 67.5,
 "sources": ["chicamasurftrips-about", "..."], "rationale": "...",
 "decided_at": "2026-10-13", "sensitivity": {"...": "..."}, "model_wind": {"...": "..."}}
```

The rule (`app.services.research.decisions.propose`, applied mechanically):

* **changed** only when at least two independent publishers whose pages were read directly
  state values that contradict the profile and agree with one new value within the
  agreement tolerances of section 2, and every directly read source supporting the old
  value is of a strictly lower tier than both of them. The new value is one a source
  states (the most central of the agreeing ones), never an average; a single direction
  never becomes a new swell window;
* **kept** when only search summaries contradict, when the directly read contradicting
  sources are of a lower tier than the directly read supporting ones, or when one publisher
  contradicts and at least two other directly read publishers of the same or a higher tier
  support the profile. A publisher that both supports and contradicts the value (one page
  or sentence each way) counts only as contradicting in these two tests: its own support
  does not outweigh its contradiction (review of 2026-10-13; this turned Anchor Point's
  wind, Lobitos' swell window and Waimea Bay's size range from kept to disputed);
* **disputed** otherwise (the contradiction is real but not resolved).

For the offshore wind every record also reports the model wind at the forecast point (GFS
and ECMWF 10-day forecasts of 2026-10-11 in the read-only database `swell_live2`): its
vector-mean direction and the share of hours within 45° of the profile's direction and of
the main contradicting one. That is a short sample 15-20 km offshore, not a climatology, and
it says which winds blow, not which are offshore; it bears on the question only where a
directly read source says the prevailing wind is offshore (`supports_change`), and it never
decides. Every record with a candidate value carries the before/after effect on the real
2026-10-11 blend run (`scripts/profile_sensitivity.py`, read-only).

Result (2026-10-13): **82 records: 4 changed, 48 disputed, 30 kept**, plus 6 geometry notes.
No wind record met the rule (11 disputed, 9 kept); the changes are Ocean Beach, Snapper
Rocks and Punta Roca tide → `all` and Cloudbreak's working range 4-20 → 2-20 ft. Where the
assessment fires a conflict rule on wind, or a record says the wind is disputed, every
forecast hour says "Sources disagree about which wind is offshore here."
(`forecast-confidence.md`, forecast-v1.1).

`research lint` also lists spots whose offshore wind is more than 60° from straight
offshore for the shore normal the model uses (the measured one, else the centre of the
optimal swell window) unless a record covers the offshore wind: a decision record, or a
geometry note with reason `wave_face` (at a point or wrapping reef the offshore wind is for
the wave face, and a directly read source supports the profile's direction) or
`stated_orientation` (a directly read source or a measurement gives the break's
orientation). Of the 19 spots over 60°, 12 have wind decision records and 6 have notes
(Rincon, Punta de Lobos, Padang Padang, Burleigh Heads, Safi: `wave_face`; Bells Beach:
`stated_orientation`); **Restaurants** remains listed, because no source describes the
reef's orientation or the wave wrapping.

## 11. Per-spot research documents

[`docs/surf_spot_research/`](surf_spot_research/README.md) has one generated file per spot
(spec section 17): the ideal conditions the evidence supports (from
`data/surf_spots/research_conditions.json`, each value citing the exact excerpts of this
file's claims, unsupported fields marked *not documented*), the conflicting and missing
information, the model settings the evidence does not support (a research field that is not
documented or conflicting, or a variable whose assessment above has no support or an
unresolved conflict), the research-confidence class of this methodology, the sources and the
last review date (newest `accessed_at`). Forecast confidence is not part of these files.
Regenerate them with `backend/.venv/bin/python scripts/generate_spot_research_docs.py`; the
unit test `test_spot_research_docs.py` fails when one is out of date.
