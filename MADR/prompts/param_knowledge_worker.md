# Parameter Knowledge for Worker Agent

## VIC Soil Parameters — Local Basin Effects

### binfilt (Variable Infiltration Curve, 0.001–0.4)
Controls how rainfall is partitioned between infiltration and surface runoff.
- **Low (0.001–0.1)**: Nearly all rain infiltrates. Very low surface runoff. Hydrograph is dominated by subsurface flow. Peaks are low and broad.
- **Medium (0.1–0.25)**: Moderate surface runoff. Good balance for most basins.
- **High (0.25–0.4)**: Most rain becomes surface runoff. Sharp, high peaks. Suitable for low-permeability or urban basins.
- **Diagnostic**: peak_ratio > 1.0 → decrease binfilt. peak_ratio < 1.0 → increase binfilt.
- **Note**: In the semi-arid Huaihe Basin, optimal binfilt is typically 0.10–0.30.

### Ds (Baseflow Non-linearity, 0.0001–1.0)
Controls the responsiveness of baseflow drainage.
- **Low (0.001–0.1)**: Baseflow is sluggish, drains slowly. Long recession curves.
- **High (0.5–1.0)**: Baseflow responds quickly to soil moisture changes. Short recession.
- **Diagnostic**: lowflow_ratio < 1.0 (simulated baseflow too low) → increase Ds. Recession too fast → decrease Ds.
- **Interaction**: Ds acts as a fraction-multiplier of Dsmax. Both together control baseflow magnitude.

### Dsmax (Maximum Baseflow Rate, 1.0–30.0 mm/day)
Upper limit on baseflow drainage rate.
- **Low (1.0–5.0)**: Very limited baseflow. Soil retains water longer.
- **Medium (5.0–15.0)**: Moderate drainage. Typical for most soils.
- **High (15.0–30.0)**: Fast drainage. Sandy or high-conductivity soils.
- **Diagnostic**: lowflow_ratio < 1.0 AND Ds already high → increase Dsmax. PBIAS > 0 (volume overprediction) → reduce Dsmax to retain more water in soil.

### Ws (Soil Moisture Threshold, 0.5–1.0)
Soil moisture fraction at which non-linear (fast) baseflow begins.
- **Low (0.5–0.6)**: Fast baseflow kicks in at moderate soil moisture. Quick drainage.
- **Medium (0.6–0.8)**: Moderate threshold. Balanced baseflow onset.
- **High (0.8–1.0)**: Soil must be nearly saturated before fast drainage starts. Delays baseflow onset, creates more storage effect.
- **Diagnostic**: Recession starts too early → increase Ws. Recession delayed too long → decrease Ws.

### soil_d2 (Layer 2 Depth, 0.2–1.5 m)
Depth of the middle soil layer. Primary control on intermediate storage.
- **Shallow (0.2–0.5)**: Less storage, flashier response, higher peaks.
- **Deep (0.8–1.5)**: More storage buffer, smoother hydrograph, lower peaks.
- **Diagnostic**: PBIAS > 0 (overprediction) → increase soil_d2 to store more water. Peaks too high → increase soil_d2.
- **Constraint**: MUST be less than soil_d3.

### soil_d3 (Layer 3 Depth, 0.5–2.0 m)
Depth of the deep soil layer. Main long-term storage reservoir.
- **Shallow (0.5–1.0)**: Limited deep storage. Baseflow depletes quickly.
- **Deep (1.5–2.0)**: Large deep reservoir. Sustained baseflow during dry periods.
- **Diagnostic**: lowflow_ratio < 1.0 during dry periods → increase soil_d3. PBIAS < 0 (volume underprediction) AND soil_d2 already reasonable → decrease soil_d3 to release stored water faster.
- **Constraint**: MUST be greater than soil_d2.

## Routing Parameters — Hydrograph Timing and Shape

### velocity (0.5–3.0 m/s)
River flow travel speed. Controls WHEN the hydrograph peak arrives.
- **Low (0.5–1.0)**: Slow travel. Peak arrives late. Flat, meandering rivers.
- **Medium (1.0–2.0)**: Typical for moderate-gradient rivers.
- **High (2.0–3.0)**: Fast travel. Peak arrives early. Steep or channelized rivers.
- **Diagnostic**: Simulated peak arrives too EARLY → decrease velocity. Too LATE → increase velocity.

### diffusivity (200–4000 m²/s)
Controls hydrograph smoothing/attenuation during routing.
- **Low (200–600)**: Minimal smoothing. Sharp peaks preserved. Small basins.
- **Medium (600–2000)**: Moderate smoothing. Medium basins.
- **High (2000–4000)**: Heavy smoothing. Peaks flattened and spread out. Large basins with floodplains.
- **Diagnostic**: Simulated peak too SHARP → increase diffusivity. Too FLAT/WIDE → decrease diffusivity.

## Common Adjustment Patterns

| Problem | Primary Fix | Secondary Fix |
|---------|-------------|---------------|
| Peak too high | Decrease binfilt | Increase soil_d2, increase diffusivity |
| Peak too low | Increase binfilt | Decrease soil_d2, decrease diffusivity |
| Low flow too low | Increase Ds or Dsmax | Increase soil_d3 |
| Low flow too high | Decrease Ds or Dsmax | Decrease soil_d3 |
| PBIAS > 0 (over) | Increase soil_d2/d3 | Decrease binfilt |
| PBIAS < 0 (under) | Decrease soil_d2/d3 | Increase binfilt |
| Peak timing early | Decrease velocity | — |
| Peak timing late | Increase velocity | — |
| Peak too sharp | Increase diffusivity | — |
| Peak too flat | Decrease diffusivity | — |
| Recession too fast (k_ratio > 1) | Increase soil_d3 | Decrease Ds |
| Recession too slow (k_ratio < 1) | Decrease soil_d3 | Increase Ds |
