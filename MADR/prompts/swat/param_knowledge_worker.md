# Parameter Knowledge for Worker Agent (SWAT+)

You are calibrating SWAT+ for one gauge. SWAT+ has integrated routing (no
separate routing step), so all 18 parameters below are tunable in every round.
You propose changes for the HRUs of your assigned subbasin only; the batched
executor handles HRU-set partitioning. The run config is the authoritative
source for parameter bounds and default centers (`param_ranges` /
`initial_param_center`); the values below mirror it.

Parameters are written to `calibration.cal`. Each line uses one of three
change-types:
- `absval` — set to this value exactly (replace).
- `abschg` — add this value to the current value (offset).
- `pctchg` — multiply current value by `(1 + value/100)` — preserves spatial
  heterogeneity (HRU-level differences from soil/landuse layers).

## The 13 Core Calibration Parameters

### cn2 (SCS Curve Number, range -25 to +25 pctchg, default 0)
Single largest driver of storm-peak magnitude. A ±10% adjustment can double
or halve peaks. Always use `pctchg` so the SSURGO/HWSD spatial pattern is
preserved across HRUs.
- Diagnostic: peak_ratio > 1.0 → decrease cn2 (negative pctchg). peak_ratio < 1.0 → increase cn2.

### esco (Soil Evaporation Compensation, range 0 to 1, change_type=absval, default 0.55)
Controls how deep soil evaporation can pull water from.
- Higher (closer to 1) → evaporation draws from deeper layers → less baseflow.
- Lower (closer to 0) → evaporation limited to topsoil → more water available for runoff.

### awc (Available Water Capacity, range -50 to +50 percent change, change_type=pctchg, default 0)
Per-layer available water capacity. Tune with `pctchg`.
- Higher (+pct) → more soil storage → smaller peaks, longer recession.
- Lower (-pct) → less storage → flashier hydrograph.

### surlag (Surface Runoff Lag, range 0.05 to 24, change_type=absval, default 4)
Controls how quickly surface runoff reaches the channel.
- Lower → flashier, sharper peaks.
- Higher → smoother, lagged peaks.

### lat_ttime (Lateral Subsurface Travel Time, range 0.5 to 180 days, change_type=absval, default 5)
Controls hillslope storm-flow timing. Larger values delay subsurface contribution to channel.

### canmx (Maximum Canopy Storage, range 0 to 100 mm, change_type=absval, default 1)
Maximum water intercepted by canopy.
- Higher → more interception loss → smaller storm peaks.

### epco (Plant ET Compensation, range 0 to 1, change_type=absval, default 0.4)
Plant water-uptake compensation across soil layers.
- Higher → plants pull from deeper layers preferentially → drier subsoil.

### perco (Percolation Coefficient, range 0 to 1, change_type=absval, default 0.85)
- Higher → more water reaches the aquifer.
- Lower → more water stays in the soil column → less baseflow, more ET.

**PERCO / DRY-BIAS RULE (empirically verified, Yellow River 1981-1990 monthly run):**
1. Overall too dry (negative PBIAS): adjust `cn2` and `esco` FIRST.
2. `awc`, `perco`, and `deep_seep` are SECONDARY levers — never adjust two or more of them simultaneously with large magnitudes without direct evidence.
3. Low flows too low: check `alpha`, `flo_min`, `deep_seep`, `revap_co` first.
4. Increase `perco` ONLY IF `deep_seep` is already very low, OR a single-parameter probe has demonstrated that raising `perco` improves NSE/J_total. With `deep_seep` > 0 the extra percolation is lost to deep storage and streamflow DECREASES.
5. Consult the round-history table and NEVER repeat a parameter direction or combination that previously worsened PBIAS/NSE/J_total. Cautionary case (round 3): `perco=0.95` combined with a large `awc` cut collapsed PBIAS from -80% to -98% (river nearly dry).

### alpha (Baseflow Alpha Factor, range 0 to 1, change_type=absval, default 0.013)
Baseflow recession constant.
- Higher → faster recession (peaky baseflow).
- Lower → slower recession (sustained baseflow).
- Diagnostic: recession too fast → decrease alpha. Too slow → increase.

### revap_co (Groundwater Revap Coefficient, range 0.02 to 0.2, change_type=absval, default 0.02)
Upward water transfer from aquifer to soil/atmosphere.
- Higher → drier baseflow regime (more revap loss).

### revap_min (Water-Table Depth Threshold for Revap, range 0 to 20 m, change_type=absval, default 10)
Water-table depth threshold for revap. When dep_wt < revap_min, revap occurs.

### flo_min (Water-Table Depth Threshold for Baseflow, range 0 to 20 m, change_type=absval, default 8)
Water-table depth threshold for return flow (baseflow). When dep_wt ≤ flo_min, baseflow occurs. NOTE: Controls BASEFLOW, NOT revap. Unit is METERS, not mm.

### deep_seep (Deep Percolation Fraction, range 0.001 to 0.4, change_type=absval, default 0.05)
Fraction of aquifer recharge that escapes downward to deep storage and leaves the shallow system. Important for fissured / karst / deep-percolation regions.

### Snow parameters
- `snofall_tmp` (-5 to 5°C, default 1.0): temp below which precip falls as snow.
- `snomelt_tmp` (-5 to 5°C, default 0.5): temp above which snowpack melts.
- `snomelt_max` (0-10 mm/°C/day, default 4.5): max melt rate (June 21).
- `snomelt_min` (0-10 mm/°C/day, default 4.5): min melt rate (Dec 21).
- `snomelt_lag` (0-1, default 1.0): snowpack temperature lag.

NOTE: `gw_delay` is DISABLED in SWAT+ source code. Recharge has NO delay. Do NOT propose `gw_delay`.

## Common Adjustment Patterns

| Problem | Primary Fix | Secondary Fix |
|---------|-------------|---------------|
| Peak too high | Decrease cn2 (pctchg negative) | Increase canmx, increase surlag |
| Peak too low | Increase cn2 | Decrease canmx |
| Low flow too low | Decrease alpha, increase flo_min | Decrease revap_co, decrease deep_seep |
| Low flow too high | Increase alpha, decrease flo_min | Increase revap_co, increase deep_seep |
| **PBIAS > 0 (OVER-prediction, sim too wet)** | Increase esco (more ET), increase awc (more storage), decrease cn2 | Increase canmx |
| **PBIAS < 0 (UNDER-prediction, sim too dry)** | Decrease esco (less ET), decrease awc (less storage), increase cn2 | Decrease canmx |
| Recession too fast | Decrease alpha | Increase awc |
| Recession too slow | Increase alpha | Decrease awc |
| Peak timing late | Decrease surlag, decrease lat_ttime | — |
| Peak timing early | Increase surlag, increase lat_ttime | — |

## Calibration Order

1. **cn2** — peak magnitude (most sensitive). Adjust FIRST with pctchg.
2. **surlag, canmx** — peak timing and storm interception.
3. **alpha, flo_min** — baseflow recession and onset.
4. **esco, epco, awc** — water balance / PBIAS.
5. **perco, revap_co** — fine-tune baseflow regime.
6. **lat_ttime, revap_min** — only if specific timing / threshold issues remain.

## Diagnostic Table

| Symptom | Likely Cause | Adjustment |
|---|---|---|
| **PBIAS > +10 %** (sim TOO WET — positive = over-prediction) | Too much runoff | Decrease `cn2` (negative pctchg), increase `esco`, increase `awc` |
| **PBIAS < -10 %** (sim TOO DRY — under-prediction) | Not enough runoff | Increase `cn2` (positive pctchg), decrease `esco`, decrease `awc` |
| Peaks too high | cn2 too high or surlag too low | Decrease `cn2`, increase `surlag` |
| Peaks too low | cn2 too low or surlag too high | Increase `cn2`, decrease `surlag` |
| Recession too fast | alpha too high | Decrease `alpha` |
| Recession too slow | alpha too low | Increase `alpha` |
| Baseflow too low | Deep losses / revap | Increase `flo_min`, decrease `deep_seep` (raise `perco` only per the perco rule) |
| Baseflow too high | Recharge / revap mismatch | Decrease `flo_min`, decrease `perco` |
| Peaks early | Surface lag too short | Increase `surlag` or `lat_ttime` |
| Peaks late | Surface lag too long | Decrease `surlag` |
