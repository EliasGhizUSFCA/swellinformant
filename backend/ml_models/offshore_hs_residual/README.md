# offshore_hs_residual v4 (exported)

Offshore significant wave height (Hm0) correction for GFS-Wave forecasts, ridge
regression on the log residual, trained **without** observation features
(`ml train --no-obs-features`) on GFS-Wave station-bulletin forecasts at the AODN Cape
Naturaliste buoy (bulletin 56006) against delayed-mode QC'd spectral Hm0, 2021-03-23 to
2021-10-22, and tested once on 2021-11-05 to 2021-12-31 (14,658 pairs: RMSE 0.331 m vs
raw 0.360 m). It passed every deployment criterion; see docs/ml-evaluation.md for the
numbers, the decision per criterion and the caveats.

**Not active by default.** At the surf spots its inputs come from gridded GFS-Wave, not
the station bulletin it was trained on; a live check found 18 % of the corrected hours
outside the factor range seen on its test set (docs/ml-evaluation.md section 10). Activate
it only after a shadow check on gridded forecasts.

* `offshore_hs_residual-v4.manifest.json`: registry record, experiment, package versions.
* `offshore_hs_residual-v4.joblib`: the artifact (sha256 in the manifest).

Import (verifies the sha256; registers the model as CANDIDATE, never ACTIVE):

```
python -m app.cli ml import-manifest ml_models/offshore_hs_residual/offshore_hs_residual-v4.manifest.json
python -m app.cli ml activate <id printed by the import>
```

Applies only to offshore Hm0 (not breaking height), only at the spots paired with the
Cape Naturaliste buoy (margaret-river-main-break, the-box, north-point-gracetown), only
at leads 3-240 h, and only to `ncep_gfs` forecasts. The joblib file is a pickle: load it
only in an environment you trust; it was written with scikit-learn 1.9.1 (recorded in the
manifest), and an import under another version logs a warning.
