# Parameter Knowledge for Worker Agent (wflow_sbm)

You are calibrating wflow_sbm for one gauge. wflow_sbm has integrated
kinematic-wave routing (no separate routing step), so all 8 parameters below
(6 land/subsurface/routing + 2 snow) are tunable in every round. You propose changes for the grid cells of your
assigned sub-basin only; the batched executor handles cell-set partitioning
(nested cell locking). The run config is the authoritative source for bounds
and default centers (`param_ranges` / `initial_param_center`); the values
below mirror it.

Parameters are written into the model's gridded `staticmaps.nc`:
- `mult` (KsatVer, f, SoilThickness) — MULTIPLY the base map cell-wise by your
  value (1.0 = no change). This preserves the base spatial pattern.
- `absval` (KsatHorFrac, N, N_River) — SET the per-cell value directly.

## Log-scaled parameters
`KsatHorFrac` (1–1000) and `KsatVer` (0.25–10) are sampled LOG-uniformly. Reason
about them in MULTIPLICATIVE steps (factors of ~2), not linear increments.
EXPLORE THE FULL RANGE including HIGH values — do NOT commit to a direction from
physical priors alone. For a peak-OVERSHOOT basin the optimum is often HIGH
`KsatHorFrac` (flattens the over-shooting peak) and HIGH `KsatVer`, not low.
Follow the empirical NSE response: if raising a parameter improves NSE, keep
raising it toward the ceiling.

## Official wflow_sbm guidance (rough reference — adapt, don't follow strictly)

The wflow_sbm documentation identifies `KsatHorFrac` (also written KsatHorFrac
/ the horizontal-to-vertical Ksat ratio) as the single most-used calibration
parameter because it controls the partition between fast storm response and
slow lateral subsurface (baseflow) flow. A common workflow is: (1) set the
baseflow fraction and peak attenuation with `KsatHorFrac`; (2) adjust
infiltration/baseflow magnitude with `KsatVer`; (3) shape the recession and
low-flow behaviour with `f` and `SoilThickness` together; (4) fine-tune peak
timing/shape with the Manning roughness `N` (land) and `N_River` (channel)
LAST. This is a guideline — adapt it to this basin, don't treat it as a fixed
recipe.

## The 8 Calibration Parameters (6 land/subsurface/routing + 2 snow)

### KsatHorFrac (horizontal/vertical Ksat ratio, 1 to 1000 absval, LOG, default 100)
Primary knob. Ratio of horizontal to vertical saturated conductivity; scales
LATERAL (hillslope) subsurface flow.
- INCREASING it LOWERS the baseflow fraction, FLATTENS peaks, speeds subsurface
  stormflow, reduces the recession slope.
- Diagnostic: recession too flat / peaks under-predicted (baseflow fraction too
  high) → DECREASE (log step). Peaks too flashy / recession too steep → INCREASE.

### KsatVer (vertical saturated conductivity multiplier, 0.25 to 10 mult, LOG, default 1.0)
Controls infiltration and baseflow MAGNITUDE.
- Higher (toward 10) → more infiltration, more baseflow, lower storm peaks.
- Lower (toward 0.25) → less infiltration, flashier/higher peaks.
- EXPLORE up to the ceiling (10): high KsatVer is often the fix for a summer
  peak OVERSHOOT. Do not clip this to low values from priors alone.

### f (KsatVer decay-with-depth exponent multiplier, 0.5 to 2 mult, default 1.0)
Controls how fast KsatVer decreases with soil depth → recession SHAPE.
- Larger → faster decay → SHORTER recession, less sustained baseflow.
- Smaller → slower decay → LONGER recession, more sustained baseflow.

### SoilThickness (soil storage depth multiplier, 0.5 to 2 mult, default 1.0)
Controls total soil STORAGE.
- Larger → more storage → attenuated/lower outflow, longer recession, delayed
  response, sustains LOW flows.
- Tune together with `f` for low-flow / seasonal-recession problems.

### N (land-surface Manning roughness, 0.04 to 0.5 absval, default 0.15)
Overland-flow resistance. Higher DAMPENS and DELAYS storm peaks (affects shape
and timing); minimal effect on baseflow or total volume.

### N_River (river-channel Manning roughness, 0.025 to 0.15 absval, default 0.075)
Channel-wave attenuation and travel time. Sets flood-peak TIMING, magnitude and
shape. Higher → slower, more attenuated flood wave at the outlet. ACTIVELY vary
`N` and `N_River` for peak timing/shape — they are easy to leave untouched.

### TT (snowfall temperature threshold, -2 to 3 °C, absval, default 0)
Precipitation falls as SNOW when air temperature < TT.
- HIGHER TT → MORE precip stored as snow → BIGGER spring-melt freshet.
- LOWER TT → less snow, more direct rain-driven runoff in winter/spring.
- Raise TT when the melt-season freshet is missing/too small in a cold basin.

### Cfmax (snowpack degree-day melt coefficient, 1.5 to 6 mm/°C/day, absval, default 3.75)
Snowmelt rate per degree-day above the melting threshold → controls freshet TIMING.
- LOWER → slower melt → freshet SPREAD later (May–June).
- HIGHER → faster/earlier melt.
- Lower Cfmax to delay a too-early/too-sharp melt; raise it to melt sooner.

## Snow guidance (snow-dominated basins)
For a MISSING or mistimed spring snowmelt freshet (too little flow in the melt
season, e.g. Apr–Jun in cold basins, while winter/early-spring sim is near
zero), tune snow: RAISE `TT` to accumulate more snow, and LOWER `Cfmax` to
slow/delay the melt into the freshet months. In snow-dominated basins
(sub-freezing winters) snow timing can DOMINATE the error — address it, don't
only tune conductivity.

## Hard rule
`N` and `N_River` must NOT be used to fix a volume (PBIAS) or baseflow-fraction
error — those are governed by `KsatHorFrac`/`KsatVer`/`SoilThickness`. Roughness
only reshapes/retimes the hydrograph. Fix the water balance and partition first.
