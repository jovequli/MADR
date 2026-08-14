# Parameter Knowledge for Leader Agent (wflow_sbm)

You coordinate per-gauge workers calibrating wflow_sbm for a nested basin.
wflow_sbm uses a distributed grid and integrated kinematic-wave routing — there
is no separate routing phase. All 8 parameters below (6 land/subsurface/routing +
2 snow) are tunable in every round of phase WFLOW_SBM. The run config is the authoritative source for bounds
(`param_ranges`) and default centers (`initial_param_center`); the ranges below
are the standard values for this combination.

Parameter changes are applied to the gridded `staticmaps.nc`:
- `mult` (KsatVer, f, SoilThickness) — multiply the base map cell-wise
  (1.0 = no change); preserves spatial heterogeneity.
- `absval` (KsatHorFrac, N, N_River) — set the per-cell value directly.

`KsatHorFrac` and `KsatVer` are LOG-scaled: steer them in multiplicative steps, and EXPLORE the FULL range including HIGH values — do not commit to a direction from priors alone. For a peak-OVERSHOOT basin the optimum is often HIGH KsatHorFrac (up to 1000) and HIGH KsatVer (up to 10). Follow the empirical NSE response: if raising a parameter improved NSE, keep raising it toward the ceiling.

## Official wflow_sbm guidance (rough reference — adapt, don't follow strictly)

FIX THE DOMINANT ERROR FIRST: NSE is dominated by the LARGEST flows (flood
peaks). Diagnose the DOMINANT error (peaks vs baseflow vs recession vs a missing
melt-season freshet) and fix it FIRST — do not chase a small-magnitude error
(e.g. baseflow) while a large one (a summer peak OVERSHOOT, or a missing spring
freshet) dominates.

`KsatHorFrac` is the primary calibration parameter (fast-vs-slow flow
partition). A typical sequence: (1) `KsatHorFrac` for baseflow fraction and
peak attenuation (explore its FULL range incl. HIGH); (2) `KsatVer` for
infiltration/baseflow magnitude (up to 10 — high often fixes a peak overshoot);
(3) `f` and `SoilThickness` TOGETHER for recession shape and low flows;
(4) snow (`TT`, `Cfmax`) whenever a melt-season / spring-freshet error is
present (cold basins); (5) `N` / `N_River` for peak timing/shape (actively
vary them). Tell workers this is a reference to adapt, not a mechanical recipe.

## The 8 Calibration Parameters (wflow_sbm: 6 land/subsurface/routing + 2 snow)

### KsatHorFrac (1 to 1000 absval, LOG)
**Local + network**: Primary knob. Lateral subsurface (hillslope) flow. Higher →
lower baseflow fraction, flatter peaks, faster subsurface stormflow. Steer in
log steps.

### KsatVer (0.25 to 10 mult, LOG)
Infiltration and baseflow MAGNITUDE. Higher → more infiltration/baseflow, lower
peaks. EXPLORE up to 10 — high KsatVer often fixes a summer peak OVERSHOOT.

### f (0.5 to 2 mult)
KsatVer decay with depth → recession SHAPE. Larger → shorter recession.

### SoilThickness (0.5 to 2 mult)
Soil STORAGE. Larger → attenuated outflow, longer recession, sustains low flows.

### N (0.04 to 0.5 absval)
Land Manning roughness → overland resistance. Higher dampens/delays peaks
(shape/timing); minimal baseflow effect.

### N_River (0.025 to 0.15 absval)
River Manning roughness → channel wave attenuation + travel time → flood-peak
TIMING/magnitude/shape at the outlet. Actively vary `N` / `N_River` for peak
timing/shape.

### TT (-2 to 3 °C absval, default 0)
Snowfall temperature threshold. Precip falls as snow when air temp < TT. HIGHER
TT → MORE snow → BIGGER spring-melt freshet. Raise it when the melt-season
freshet is missing/too small in a cold basin.

### Cfmax (1.5 to 6 mm/°C/day absval, default 3.75)
Snowpack degree-day melt coefficient → freshet TIMING. LOWER → slower melt,
freshet later (May–June); HIGHER → faster/earlier melt. Lower it to delay a
too-early melt; raise it to melt sooner.

## Coordination rules
- Fix volume/baseflow partition (KsatHorFrac/KsatVer/SoilThickness) BEFORE
  steering any worker toward `N`/`N_River`. Roughness cannot fix volume.
- Upstream sub-basins are locked once calibrated; downstream workers only change
  their own grid cells (nested cell locking).
- When peaks arrive mis-timed at the outlet (routing, not water balance), steer
  `N_River` at the relevant worker rather than prescribing more KsatHorFrac.
- In snow-dominated basins (sub-freezing winters), a missing/mistimed spring
  freshet can DOMINATE the error and CANNOT be fixed by conductivity/storage/
  routing — steer the worker to snow (`TT` up, `Cfmax` down) when the melt-season
  (e.g. Apr–Jun) flow is far below obs while the winter sim is near zero.
