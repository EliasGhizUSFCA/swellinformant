"""Tide predictions and spot observations.

* :mod:`.api`: the tide and observation blocks served per spot (``/api/spots/{slug}/tides``,
  ``/api/spots/{slug}/observations`` and the timeline);
* :mod:`.stage`: tide stage (0 at low water, 1 at high water) and rising/falling at given
  times, for the quality score;
* :mod:`.ingest`: the daily ``tides.acquire`` job (TIDE_PROVIDERS);
* :mod:`.maintenance` and :mod:`.health`: retention and data-health hooks.

Until the providers are implemented these return explicit "unavailable" or empty results:
no tide value is ever made up.
"""
