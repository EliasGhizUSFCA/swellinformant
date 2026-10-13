# offshore_hs_residual: exported corrections of offshore Hm0

Both file pairs here are corrections of OFFSHORE significant wave height (spectral Hm0)
for GFS-Wave forecasts (`ncep_gfs` only), trained on the log residual against buoys. They
never correct breaking (surf) height directly; the nearshore model runs on the corrected
offshore value. The `.joblib` files are pickles: load them only in an environment you
trust. Import verifies the sha256 and registers a model as CANDIDATE, never as ACTIVE.

## v6: random forest, NDBC + AODN (2026-10-12) — approved for activation

* Trained (`ml train --no-obs-features`, rolling-origin folds, chronological 60/20/20 split
  by cycles with 312 h gaps) on GFS-Wave station-bulletin forecasts against spectral Hm0
  at five NDBC buoys (51201, 51205, 46026, 46224, 46053; 2024-10 to 2025-09) and three
  AODN delayed-QC buoys (Cape Naturaliste, Rottnest Island 02, Esperance 04; 2021).
  Held-out test 2025-10-05 to 2026-10-12, 139,257 pairs at the NDBC buoys: RMSE 0.427 ->
  0.320 m (-25.0 %, 90 % CI of the difference -0.115 to -0.098 m), bias -0.256 -> +0.086
  m, HH 0.287 -> 0.192. Passed every deployment criterion (a)-(f) and the ADR-013
  transfer check on the real 2026-10-11 GFS run (0 % of the corrected spot hours outside
  the central 90 % of the test factors). Numbers, folds and caveats:
  docs/ml-evaluation.md section 12.4; decision: docs/decisions.md ADR-020.
* **Applies only to** jaws-peahi, mavericks, ocean-beach-sf, pipeline, sunset-beach,
  trestles and waimea-bay, at leads 3-240 h. Not to rincon (no held-out benefit at its
  buoy 46053; excluded with `ml restrict`), not to the Western Australian spots (their
  buoys' 2021 data were training data only, never tested: `no_held_out_test_pairs`), and
  not to any other spot. Factors on the live run: 1.10-1.61.
* Shipped re-serialised with joblib zlib compression (`ml export-manifest --compress`,
  3.7 MB instead of 9.8 MB); the manifest records the registry's original sha256 as
  `source_sha256`, and the compressed model gives the same factors (checked on a probe
  matrix and on the live run).
* Review caveats (2026-10-13, docs/ml-evaluation.md 12.4): the held-out bias of +0.086 m
  comes from the metre scale (1.1, at its clamp) chosen before the refit; and the swell
  features were the bulletin's first listed partition (the wind sea in 16 % of the test
  rows) while the spots supply the gridded primary swell. With the swell partition
  substituted the held-out RMSE is 0.342 m instead of 0.320 m (raw 0.427 m).
* Written with scikit-learn 1.9.1 (recorded in the manifest); an import under another
  version logs a warning, and the model should then be re-evaluated before activation.

Activation is an operator step:

```
python -m app.cli ml import-manifest ml_models/offshore_hs_residual/offshore_hs_residual-v6.manifest.json
python -m app.cli ml activate <id printed by the import>
python -m app.cli ml deactivate offshore_hs_residual      # back to raw forecasts at any time
```

The version number is the next free one in the importing database, so the label there may
differ from `v6`. Corrections apply to runs built after activation (a blend built before
is not rebuilt, ADR-012). With the correction active, the offshore part of the 80 % range
around the corrected value stays labelled heuristic (docs/ml-evaluation.md 12.2).

## v4: ridge, Cape Naturaliste (2026-10-11) — candidate, not active

Ridge regression trained **without** observation features on GFS-Wave station-bulletin
forecasts at the AODN Cape Naturaliste buoy (bulletin 56006) against delayed-mode QC'd
spectral Hm0, 2021-03-23 to 2021-10-22, and tested once on 2021-11-05 to 2021-12-31 (14,658
pairs: RMSE 0.331 m vs raw 0.360 m). It passed every deployment criterion but stays
inactive: at the surf spots' gridded inputs a live check found 18 % of the corrected hours
outside the factor range seen on its test set (docs/ml-evaluation.md section 10,
ADR-013). It would apply only at margaret-river-main-break, the-box and
north-point-gracetown, leads 3-240 h. Only one version of a name can be active, so
activating v4 would retire v6.

* `offshore_hs_residual-v4.manifest.json`, `offshore_hs_residual-v4.joblib` (sha256 in the
  manifest).
