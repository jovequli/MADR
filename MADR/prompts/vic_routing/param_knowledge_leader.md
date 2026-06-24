# Parameter Knowledge for Leader Agent

## VIC Soil Parameters — Cross-Basin Impact

### binfilt (Variable Infiltration Curve, 0.001–1.0)
**Local**: Controls rainfall partitioning between infiltration and surface runoff. Higher values increase the fraction of rainfall that becomes direct surface runoff, producing sharper, higher peaks.
**Cross-basin**: When an upstream basin has high binfilt, it generates flashy runoff that arrives at downstream basins as sharp inflow peaks. This can make the downstream peak_ratio appear too high even if the downstream basin's own parameters are well-calibrated. If a downstream basin consistently overpredicts peaks, check whether upstream binfilt is too high before adjusting the downstream basin.

### Ds (Baseflow Non-linearity, 0.001–1.0)
**Local**: Fraction of Dsmax where non-linear baseflow begins. Higher values mean baseflow responds more quickly to soil moisture changes, producing shorter recession curves.
**Cross-basin**: Higher Ds in upstream basins means more sustained baseflow contribution to downstream. This raises the downstream low-flow component. If downstream lowflow_ratio > 1.0 and the downstream basin's own Ds is already low, the upstream Ds may be the cause.

### Dsmax (Maximum Baseflow Rate, 1.0–30.0 mm/day)
**Local**: Upper limit on baseflow drainage rate from the lowest soil layer. Higher values allow faster drainage, increasing baseflow magnitude.
**Cross-basin**: High Dsmax upstream generates stronger sustained flow that adds to downstream baseflow. In large nested basins, the cumulative effect of multiple upstream sub-basins with high Dsmax can significantly raise downstream baseflow beyond what the downstream basin's own parameters control.

### Ws (Soil Moisture Threshold, 0.001–1.0)
**Local**: Soil moisture fraction at which non-linear (fast) baseflow begins. Higher values delay the onset of rapid baseflow until soils are nearly saturated, creating more storage effect.
**Cross-basin**: Primarily affects recession curve timing, which propagates downstream but is less impactful than binfilt or Ds.

### soil_d2 (Layer 2 Depth, 0.2–1.5 m)
**Local**: Depth of the middle soil layer, primary control on intermediate storage. Deeper = more water storage, smoother hydrograph, reduced peak response.
**Cross-basin**: Deeper soil layers buffer rainfall, producing smoother outflow. Upstream basins with deeper soils send more attenuated flow downstream, making downstream hydrographs easier to fit.
**Constraint**: Must be < soil_d3.

### soil_d3 (Layer 3 Depth, 0.5–2.0 m)
**Local**: Depth of the deep soil layer. Primary long-term storage reservoir for baseflow generation. Deeper = more total storage, sustained baseflow during dry periods.
**Cross-basin**: Deeper layer 3 increases baseflow duration and magnitude in downstream contributions. Critical for matching low-flow periods at downstream stations.
**Constraint**: Must be > soil_d2.

## Routing Parameters — Network-Level Interactions

### velocity (0.5–5.0 m/s)
**Local**: Controls the speed at which water travels through the river network. Higher velocity = faster hydrograph arrival, sharper peak.
**Cross-basin (CRITICAL)**: This is the most network-sensitive parameter. Velocity controls WHEN upstream flow arrives at downstream stations. If upstream velocity is too high, the downstream station sees a premature peak. If too low, the peak arrives late. When multiple upstream basins have different velocities, their contributions may constructively interfere (amplifying peaks) or destructively interfere (smoothing peaks) at the downstream station.
**Phase interaction**: In VIC_ONLY phase, velocity is at default (1.5 m/s). The outlet NSE will be limited by timing mismatches — this is expected and will be corrected in ROUTING_ONLY phase.

### diffusivity (200–3000 m²/s)
**Local**: Controls hydrograph smoothing/attenuation during routing. Higher = more smoothing (lower, wider peaks).
**Cross-basin**: Affects how peaked the upstream contribution is when it arrives downstream. High diffusivity upstream produces gentle inflows that are easier for downstream calibration. Low diffusivity preserves sharp pulses.
**Phase interaction**: In VIC_ONLY phase, diffusivity is at default (800 m²/s). Peak shape at outlet may be imperfect — expected.

## Strategic Guidance

### When All Basins Show Similar Issues
If most sub-basins show the same problem (e.g., all have peak_ratio > 1.0), this suggests a systematic parameter issue. Recommend uniform adjustments to all workers rather than basin-specific fine-tuning.

### When Upstream Performs Well but Downstream Performs Poorly
Common causes:
1. **Routing mismatch**: Timing/smoothing parameters are wrong — will be addressed in ROUTING_ONLY phase
2. **Error accumulation**: Multiple upstream flows compound small errors at the downstream confluence
3. **Basin heterogeneity**: The downstream basin's own grid cells may have very different hydrology

### Search Radius Strategy
- **Shrink** when: NSE > 0.6 and trending upward, parameters are near optimum
- **Expand** when: NSE < 0.4 or declining, need to explore new parameter regions
- **Keep** when: moderate performance with steady improvement

### Oscillation Detection
If a basin's NSE alternates between improving and declining across rounds (e.g., +0.03, -0.02, +0.03, -0.02), the parameters are oscillating around a local optimum. This signals convergence — recommend shrinking the search radius or advancing to the next phase.

## Parameter Sensitivity Ranking (Huaihe Basin)

In the semi-arid Huaihe Basin, parameter sensitivity follows this order:
1. **binfilt** — Controls flood peaks. Adjust FIRST. Most sensitive parameter.
2. **Dsmax / Ds** — Controls recession curve and baseflow. Second priority.
3. **Ws** — Controls baseflow onset. Relatively insensitive in semi-arid conditions.
4. **soil_d2 / soil_d3** — Controls storage. Least sensitive but important for long-term water balance.

## Huaihe Basin Priors

- All 5 stations share semi-arid climate (Gou et al. 2020)
- Headwaters XX, ZK, JJJ should have SIMILAR optimal binfilt (typically 0.10–0.30)
- Soil depths (d2, d3) may differ slightly due to geology but are often in the same range
- Routing velocity and diffusivity depend on channel characteristics — DO NOT share routing priors across sub-basins
- If one headwater converges quickly, its optimal binfilt is a strong prior for the others
- Ds and Ws are relatively insensitive in this basin (soil rarely saturates)

## Upstream Error Propagation

Volume errors compound roughly linearly by contributing area:
- If XX PBIAS=+10% (area fraction ~0.15 of WJB) and ZK PBIAS=+5% (fraction ~0.19), WJB sees ~+2.5% inflow bias before its own local contribution
- Upstream PBIAS < ±5%: acceptable, no action needed
- Upstream PBIAS ±5–10%: warn downstream workers
- Upstream PBIAS > ±10%: downstream calibration will be unreliable

Timing errors are HARDER to compensate downstream:
- If XX velocity too high → peaks arrive at WJB too early → WJB cannot fix by adjusting its own velocity

## Extended Metrics Interpretation

### peak_timing_bias_days
- Negative = sim peaks arrive early → velocity too high, decrease velocity
- Positive = sim peaks arrive late → velocity too low, increase velocity
- Use this metric primarily in ROUTING_ONLY and JOINT phases

### peak_magnitude_bias_pct
- Positive = sim peaks too high → binfilt too high or diffusivity too low
- Negative = sim peaks too low → binfilt too low or diffusivity too high
- Cross-check with PBIAS: if both positive, it's a volume issue (decrease binfilt)

### recession_k_ratio
- > 1.0 = sim recession too fast → needs more storage (increase soil_d3) or slower drainage (decrease Ds)
- < 1.0 = sim recession too slow → reduce storage or increase drainage
- Most useful in VIC_ONLY and JOINT phases

### baseflow_index_bias_pct
- Negative = sim baseflow too low → increase soil_d3 for deeper storage, or decrease Ds for slower drainage
- Positive = sim baseflow too high → decrease soil_d3, increase Ds
- Directly related to dry-season flow performance
