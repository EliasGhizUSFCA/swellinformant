"""Buoy observations: station catalog, provider adapters (AODN, Met Office, NDBC), ingestion.

Only real measurements are stored here. Every record keeps what its numbers mean (which
significant wave height definition, which period, the provider's QC verdict), so offshore
model output is never silently compared with a different quantity. See docs/observations.md.
"""
