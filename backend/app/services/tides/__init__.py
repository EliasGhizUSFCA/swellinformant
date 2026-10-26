"""Tide predictions and spot observations (docs/tides.md, docs/observations.md).

* :mod:`.coops`: NOAA CO-OPS predictions (US stations) and the subordinate-station curve;
* :mod:`.harmonic_model`: harmonic synthesis from the committed EOT20 constants (no network);
* :mod:`.catalog`: data/tide_stations.json, the tide point(s) of every spot;
* :mod:`.ingest`: the daily ``tides.acquire`` job (TIDE_PROVIDERS);
* :mod:`.api`: the tide and observation blocks served per spot (``/api/spots/{slug}/tides``,
  ``/api/spots/{slug}/observations`` and the timeline);
* :mod:`.stage`: tide stage (0 at low water, 1 at high water) and rising/falling at given
  times, for the quality score;
* :mod:`.maintenance` and :mod:`.health`: retention and data-health hooks;
* :mod:`.cli`: ``python -m app.cli tides seed|acquire|list|prune``.

Where nothing is stored or configured the blocks say "unavailable": no tide value is ever
made up.
"""
