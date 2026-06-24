# Parameter Knowledge for Leader Agent (VIC + CaMa-Flood)

## VIC Soil Parameters — Cross-Basin Impact

### binfilt (Variable Infiltration Curve, 0.001–0.4)
**Local**: Controls rainfall partitioning between infiltration and surface runoff. Higher values increase the fraction of rainfall that becomes direct surface runoff, producing sharper, higher peaks.
**Cross-basin**: When an upstream basin has high binfilt, it generates flashy runoff that arrives at downstream basins as sharp inflow peaks. This can make the downstream peak_ratio appear too high even if the downstream basin's own parameters are well-calibrated. If a downstream basin consistently overpredicts peaks, check whether upstream binfilt is too high before adjusting the downstream basin.

### Ds (Baseflow Non-linearity, 0.0001–1.0)
**Local**: Fraction of Dsmax where non-linear baseflow begins. Higher values mean baseflow responds more quickly to soil moisture changes, producing shorter recession curves.
**Cross-basin**: Higher Ds in upstream basins means more sustained baseflow contribution to downstream. This raises the downstream low-flow component.

### Dsmax (Maximum Baseflow Rate, 1.0–30.0 mm/day)
**Local**: Upper limit on baseflow drainage rate from the lowest soil layer.
**Cross-basin**: High Dsmax upstream generates stronger sustained flow that adds to downstream baseflow.

### Ws (Soil Moisture Threshold, 0.5–1.0)
**Local**: Soil moisture fraction at which non-linear (fast) baseflow begins.
**Cross-basin**: Primarily affects recession curve timing; less impactful than binfilt or Ds.

### soil_d2 (Layer 2 Depth, 0.1–2.0 m)
**Local**: Middle-layer storage. Higher values absorb more rainfall before runoff begins, smoothing peaks.
**Cross-basin**: Limited downstream effect; mostly controls local water balance.

### soil_d3 (Layer 3 Depth, 0.5–2.0 m, MUST > soil_d2)
**Local**: Deep-layer storage and baseflow reservoir.
**Cross-basin**: Sustained dry-season flow from upstream depends on soil_d3. Critical for downstream low-flow continuity.

## CaMa-Flood Channel Parameters — River-network Routing

CaMa-Flood routes VIC runoff through a global river network with explicit
floodplain inundation. Channel cross-section is derived from climatological
Qave per cell using power laws:

- Depth `H = max(Hmin, Hc · Qave^0.5)`
- Width `W = max(Wmin, Wc · Qave^0.6)`

You tune the 4 coefficients/floors. Other channel parameters (manning,
floodplain elevation profile, bifurcation) stay at defaults baked into the
prepared `bengbu_15min` basin map.

### Hc (Channel Depth Coefficient, 0.01–1)
**Local**: Scales channel depth with discharge climatology. Higher Hc → deeper channels everywhere → more in-channel volume before overbank; slower kinematic wave (slower peak arrival, more attenuation).
**Cross-basin**: Hc operates on the whole network. Increasing it slows large rivers more than small ones because `Qave^0.5` is larger downstream. A single basin's tuning of Hc affects the routing of all upstream contributions.

### Hmin (Minimum Channel Depth, 0.5–2 m)
**Local**: Floor on channel depth in headwater / low-Qave cells.
**Cross-basin**: Higher Hmin slows down small-stream routing without changing main-stem behaviour. Useful when headwater hydrographs arrive too sharply at the confluence.

### Wc (Channel Width Coefficient, 0.1–4)
**Local**: Dominant lever on channel storage. Wider channels store more water and attenuate flood peaks substantially. CaMa default is 2.5.
**Cross-basin**: Wide-channel networks store flood waves longer and release them more gradually. Reducing Wc network-wide makes the system more flashy at all scales; increasing Wc dampens peaks and lengthens recession.

### Wmin (Minimum Channel Width, 1–10 m)
**Local**: Floor on width in small streams.
**Cross-basin**: Mostly affects headwater behaviour. Higher Wmin makes small streams less flashy and improves low-flow stage continuity in tributaries.

## Strategic Guidance

### When All Basins Show Similar Issues
If most sub-basins show the same problem (e.g., all have peak_ratio > 1.0), this suggests a systematic parameter issue. Recommend uniform VIC adjustments to all workers, or a global channel adjustment, rather than basin-specific fine-tuning.

### When Upstream Performs Well but Downstream Performs Poorly
Common causes in the VIC+CaMa-Flood chain:
1. **Channel mismatch**: Hc / Wc are wrong for the main stem — will be addressed in CAMA_FLOOD_ONLY phase.
2. **Error accumulation**: Multiple upstream flows compound small errors at the downstream confluence.
3. **Floodplain storage**: Large downstream flood events get attenuated more than VIC+Lohmann would have predicted; this is the normal CaMa-Flood behaviour, not necessarily a bug.

### Search Radius Strategy
- **Shrink** when NSE > 0.6 and trending upward.
- **Expand** when NSE < 0.4 or declining.

### Oscillation Detection
If a basin's NSE alternates between improving and declining across rounds, recommend shrinking the search radius or advancing to the next phase.

## Parameter Sensitivity Ranking (Huaihe Basin)

VIC side (semi-arid):
1. **binfilt** — Controls flood peaks. Adjust FIRST.
2. **Dsmax / Ds** — Controls recession and baseflow.
3. **Ws, soil_d2, soil_d3** — Long-term water balance.

CaMa channel side:
1. **Wc** — Dominant storage lever. Adjust first.
2. **Hc** — Wave-speed driver. Adjust next.
3. **Wmin, Hmin** — Headwater-specific tuning.

## Huaihe Basin Priors

- Semi-arid; all 5 stations share climate.
- Headwaters XX, ZK, JJJ likely share optimal binfilt (typically 0.10–0.30).
- Channel params (Hc/Hmin/Wc/Wmin) are GLOBAL in CaMa-Flood — they apply network-wide; do not propose basin-specific values for channel params. Worker-specific channel proposals should be averaged or only the outlet (BB) should drive channel tuning.

## Upstream Error Propagation

Volume errors compound by contributing area, but CaMa-Flood adds floodplain storage which can reshape error propagation:
- A wet bias upstream may be partially absorbed by floodplain inundation at downstream if Wc is high → it shows up as delayed arrival rather than larger peak.
- A dry bias propagates almost linearly because dry channels stay in-bank and lose nothing to the floodplain.

## Extended Metrics Interpretation

### peak_timing_bias_days
- Negative = sim peaks arrive early → channel too fast → increase Hc or Hmin.
- Positive = sim peaks arrive late → channel too slow → decrease Hc or Hmin.
- Use this metric primarily in CAMA_FLOOD_ONLY and JOINT phases.

### peak_magnitude_bias_pct
- Positive = sim peaks too high → binfilt too high OR Wc too low (insufficient attenuation).
- Negative = sim peaks too low → binfilt too low OR Wc too high.
- Cross-check with PBIAS: if both positive, it's a volume issue (decrease binfilt) — not a channel issue.

### recession_k_ratio
- > 1.0 = sim recession too fast → increase soil_d3 or Wmin.
- < 1.0 = sim recession too slow → reduce soil_d3 or Wmin.

### baseflow_index_bias_pct
- Negative = sim baseflow too low → increase soil_d3 or Ds. Channel side: Wmin can also help by keeping small streams populated.
- Positive = sim baseflow too high → decrease soil_d3 or Ds.
