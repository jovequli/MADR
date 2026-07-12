# Parameter Knowledge for Leader Agent (SWAT+)

You coordinate per-gauge workers calibrating SWAT+ for a nested basin. SWAT+
uses HRUs (Hydrologic Response Units) and integrated routing — there is no
separate routing phase. All 18 parameters below are tunable in every round
of phase SWATPLUS_HRU. The run config is the authoritative source for
parameter bounds (`param_ranges`) and default centers (`initial_param_center`);
the ranges below are the standard values for this combination.

Parameter changes are applied via `calibration.cal`. The three change-types:
- `absval` — set to this value exactly.
- `abschg` — add this value to the current value.
- `pctchg` — multiply current value by `(1 + value/100)` — preserves
  spatial heterogeneity, mandatory for spatially varying params like
  `cn2`, `awc`, `sol_k`.

## The 18 Calibration Parameters (SWAT+)

### cn2 (SCS Curve Number, -25 to +25 pctchg)
**Local**: Largest driver of storm-peak magnitude per HRU.
**Cross-basin**: When an upstream basin has cn2 too high, it generates flashy
runoff that arrives at downstream basins as sharp inflow peaks. If a
downstream basin consistently overpredicts peaks, check whether upstream cn2
is too high before adjusting the downstream basin.

### esco (Soil Evap Compensation, 0 to 1, absval)
**Local**: Higher → ET draws from deeper layers → less baseflow.
**Cross-basin**: Lower esco upstream means more soil moisture exits as
baseflow rather than ET, raising downstream low-flow contribution.

### awc (Available Water Capacity, ±50%, pctchg)
**Local**: Storage capacity per soil layer. Higher → more buffering, lower peaks.
**Cross-basin**: Upstream basins with high awc send smoother hydrographs
downstream — easier to fit at downstream gauges.

### surlag (Surface Runoff Lag, 0.05 to 24, absval)
**Local**: Controls how quickly surface runoff reaches the channel.
**Cross-basin**: Affects timing of upstream peaks arriving at downstream. If
upstream surlag is too small, downstream sees premature peaks.

### lat_ttime (Lateral Subsurface Travel Time, 0.5 to 180 days, absval)
Hillslope subsurface timing. Less network-sensitive than surlag.

### canmx (Maximum Canopy Storage, 0 to 100 mm, absval)
Higher → more interception loss → smaller storm peaks. Affects upstream peak
magnitude.

### epco (Plant ET Compensation, 0 to 1, absval)
Higher → plants pull water from deeper layers preferentially.

### perco (Percolation Coefficient, 0 to 1, absval)
**Local**: Higher → more water reaches the aquifer.
**Cross-basin**: Strongly affects downstream baseflow contribution.

**PERCO / DRY-BIAS RULE (empirically verified, Yellow River 1981-1990 monthly run):**
1. Overall too dry (negative PBIAS): adjust `cn2` and `esco` FIRST.
2. `awc`, `perco`, and `deep_seep` are SECONDARY levers — never adjust two or more of them simultaneously with large magnitudes without direct evidence.
3. Low flows too low: check `alpha`, `flo_min`, `deep_seep`, `revap_co` first.
4. Increase `perco` ONLY IF `deep_seep` is already very low, OR a single-parameter probe has demonstrated that raising `perco` improves NSE/J_total. With `deep_seep` > 0 the extra percolation is lost to deep storage and streamflow DECREASES.
5. Consult the round-history table and NEVER repeat a parameter direction or combination that previously worsened PBIAS/NSE/J_total. Cautionary case (round 3): `perco=0.95` combined with a large `awc` cut collapsed PBIAS from -80% to -98% (river nearly dry).

### alpha (Baseflow Alpha, 0 to 1, absval)
**Local**: Recession constant. Higher → faster recession.
**Cross-basin**: Lower alpha upstream gives more sustained baseflow.

### revap_co (Groundwater Revap, 0.02 to 0.2, absval)
Higher → drier baseflow regime. Upstream effect on downstream baseflow magnitude.

### revap_min (Revap Water-Table Threshold, 0 to 20 m, absval)
Water-table depth threshold for revap activation. When dep_wt < revap_min, revap occurs.

### flo_min (Baseflow Water-Table Threshold, 0 to 20 m, absval)
Water-table depth threshold for baseflow / return flow. NOTE: Controls baseflow, NOT revap. When dep_wt ≤ flo_min, baseflow occurs.

### deep_seep (Deep Percolation Fraction, 0.001 to 0.4, absval)
Fraction of aquifer recharge that escapes downward to deep storage. Matters most in fissured / karst / deep-percolation regions where real deep losses are non-negligible.

### Snow parameters (Tangnaihai only)
- `snofall_tmp` (-5 to 5°C): temperature below which precip is snow.
- `snomelt_tmp` (-5 to 5°C): temperature above which snowpack melts.
- `snomelt_max` (0-10 mm/°C/day): maximum melt rate (summer solstice).
- `snomelt_min` (0-10 mm/°C/day): minimum melt rate (winter solstice).
- `snomelt_lag` (0-1): snowpack temperature lag factor.

NOTE: `gw_delay` is DISABLED in SWAT+ source. Do NOT propose it.

## Strategic Guidance

### When All Basins Show Similar Issues
If most sub-basins share the same problem (e.g., all peak_ratio > 1.0),
recommend a uniform pctchg negative on cn2 across all workers rather than
basin-specific tuning.

### When Upstream Performs Well but Downstream Performs Poorly
Common causes for SWAT+:
1. **Error accumulation**: multiple upstream flows compound small errors at
   the downstream confluence.
2. **Channel routing in the downstream reach**: rare in SWAT+ (integrated)
   but possible via `CH_N2` (Manning's n) — not in this 18-param set.
3. **Basin heterogeneity**: the downstream basin's own HRUs may have very
   different hydrology.

### Search Radius Strategy
- **Shrink** when: NSE > 0.6 and trending upward.
- **Expand** when: NSE < 0.4 or declining.
- **Keep** when: moderate, steady improvement.

### Oscillation Detection
If a basin's NSE alternates up/down across rounds, parameters are
oscillating around a local optimum. Shrink the radius or advance to a
finer-grained tuning.

## Parameter Sensitivity Ranking

cn2 > esco > awc > alpha ≈ surlag > perco > epco > flo_min > deep_seep > lat_ttime > revap_co > revap_min > canmx (+ snow params in snow-dominated basins)

1. **cn2** — flood peaks. Adjust FIRST with pctchg.
2. **esco** — water-balance / PBIAS.
3. **awc** — soil storage and peak smoothing (pctchg).
4. **alpha, surlag** — recession and peak timing.
5. **perco, epco** — baseflow partition and ET.
6. **flo_min, deep_seep, lat_ttime, revap_co, revap_min, canmx** — fine-tune.

## Initial Parameter Priors

- Use the basin's SSURGO/HWSD-derived cn2 and awc fields as the round-0 baseline.
- Headwater sub-basins often need similar cn2 pctchg.
- Lowland sub-basins usually need smaller alpha (slower baseflow) and larger flo_min (more sustained baseflow).

## Upstream Error Propagation

Volume errors compound roughly linearly by contributing area:
- Upstream PBIAS < ±5%: acceptable.
- Upstream PBIAS ±5–10%: warn downstream workers.
- Upstream PBIAS > ±10%: downstream calibration will be unreliable.

## Extended Metrics Interpretation

- **peak_magnitude_bias_pct**: positive → cn2 too high, or canmx too low.
- **recession_k_ratio**: > 1.0 → alpha too high (recession too fast).
- **baseflow_index_bias_pct**: negative → perco too low or revap_co too high (raise `perco` only per the perco rule).
- **peak_timing_bias_days**: negative → surlag / lat_ttime too low.

## ⚠ PBIAS sign convention

The framework computes `PBIAS = 100 × Σ(sim - obs) / Σ(obs)`.

- **PBIAS > 0 → OVER-prediction (sim too WET)** — needs LESS runoff
- **PBIAS < 0 → UNDER-prediction (sim too DRY)** — needs MORE runoff

## Diagnostic Table

| Symptom | Likely Cause | Adjustment |
|---|---|---|
| **PBIAS > +10 %** (sim TOO WET — over-prediction) | Too much runoff, not enough ET | Decrease `cn2` (negative pctchg), increase `esco`, increase `awc` |
| **PBIAS < -10 %** (sim TOO DRY — under-prediction) | Not enough runoff, too much ET | Increase `cn2` (positive pctchg), decrease `esco`, decrease `awc` |
| Peaks too high | cn2 too high or surlag too low | Decrease `cn2`, increase `surlag` |
| Peaks too low | cn2 too low or surlag too high | Increase `cn2`, decrease `surlag` |
| Recession too fast | alpha too high | Decrease `alpha` |
| Recession too slow | alpha too low | Increase `alpha` |
| Baseflow too low | Deep losses / revap | Increase `flo_min`, decrease `deep_seep` (raise `perco` only per the perco rule) |
| Baseflow too high | Recharge / revap mismatch | Decrease `flo_min`, decrease `perco` |
| Peaks early | Surface lag too short | Increase `surlag` or `lat_ttime` |
| Peaks late | Surface lag too long | Decrease `surlag` |
