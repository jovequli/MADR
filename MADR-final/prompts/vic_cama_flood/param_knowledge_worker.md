# Parameter Knowledge for Worker Agent (VIC + CaMa-Flood)

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
- **Diagnostic**: lowflow_ratio < 1.0 → increase Ds. Recession too fast → decrease Ds.
- **Interaction**: Ds acts as a fraction-multiplier of Dsmax.

### Dsmax (Maximum Baseflow Rate, 1.0–30.0 mm/day)
Upper limit on baseflow drainage rate.
- **Low (1.0–5.0)**: Very limited baseflow. Soil retains water longer.
- **Medium (5.0–15.0)**: Moderate drainage.
- **High (15.0–30.0)**: Fast drainage.
- **Diagnostic**: lowflow_ratio < 1.0 AND Ds already high → increase Dsmax. PBIAS > 0 → reduce Dsmax.

### Ws (Soil Moisture Threshold, 0.5–1.0)
Soil moisture fraction at which non-linear (fast) baseflow begins.
- **Low (0.5–0.6)**: Fast baseflow kicks in at moderate soil moisture.
- **High (0.8–1.0)**: Soil must be nearly saturated before fast drainage starts.
- **Diagnostic**: Recession starts too early → increase Ws. Delayed too long → decrease Ws.

### soil_d2 (Layer 2 Depth, 0.2–1.5 m)
Depth of the middle soil layer.
- **Shallow (0.2–0.5)**: Less storage, flashier response.
- **Deep (0.8–1.5)**: More storage buffer, smoother hydrograph.
- **Diagnostic**: PBIAS > 0 → increase soil_d2. Peaks too high → increase soil_d2.
- **Constraint**: MUST be less than soil_d3.

### soil_d3 (Layer 3 Depth, 0.5–2.0 m)
Depth of the deep soil layer; long-term storage reservoir.
- **Shallow (0.5–1.0)**: Limited deep storage. Baseflow depletes quickly.
- **Deep (1.5–2.0)**: Large deep reservoir.
- **Diagnostic**: lowflow_ratio < 1.0 during dry periods → increase soil_d3.
- **Constraint**: MUST be greater than soil_d2.

## CaMa-Flood Channel Parameters — River-network Storage & Wave Speed

Channel cross-section is parameterised by:
- Depth: `H = max(Hmin, Hc · Qave^Hp)` with `Hp = 0.5` (default, not tuned).
- Width: `W = max(Wmin, Wc · Qave^Wp)` with `Wp = 0.6` (default, not tuned).

You only tune the coefficients (`Hc`, `Wc`) and the floors (`Hmin`, `Wmin`). The
power exponents and offsets stay at CaMa-Flood defaults.

### Hc (Depth Coefficient, 0.01–1)
Multiplies the climatological mean discharge `Qave` to set channel depth.
- **Low (0.01–0.1)**: Shallow channels even on large rivers. Faster kinematic wave; peaks arrive earlier and sharper.
- **Medium (0.1–0.5)**: Realistic depths for moderate basins.
- **High (0.5–1.0)**: Deep channels; more in-channel storage; slower, smoother routing.
- **Diagnostic**: Peaks arriving too early → increase Hc. Floods over-attenuated → decrease Hc.

### Hmin (Minimum Channel Depth, 0.5–2 m)
Floor on channel depth so headwater cells don't collapse.
- **Low (0.5–0.8)**: Headwater channels can be very shallow. Faster small-stream response.
- **High (1.5–2.0)**: Even tiny channels have meaningful depth, more storage in the upper network.
- **Diagnostic**: Upstream peaks arrive too early → increase Hmin. Low-flow stage too low → increase Hmin.

### Wc (Width Coefficient, 0.1–4)
Multiplies `Qave^Wp` to set channel width — the dominant lever on in-channel storage volume.
- **Low (0.1–0.5)**: Very narrow channels. Little storage. Sharp peaks, propagate fast.
- **Medium (1.0–2.5)**: Balanced. Default CaMa value is 2.5.
- **High (3.0–4.0)**: Wide channels, large storage, peaks attenuated, baseflow more sustained.
- **Diagnostic**: Peaks too sharp → increase Wc. Peaks too flat / volume too smeared → decrease Wc.

### Wmin (Minimum Channel Width, 1–10 m)
Floor on channel width — controls small-stream behaviour at low Qave.
- **Low (1–3)**: Headwater channels can be very narrow.
- **High (7–10)**: All channels stay relatively wide; useful when small-stream baseflow is underpredicted.
- **Diagnostic**: Low-flow ratio too low only in headwaters → increase Wmin. Headwater peaks unrealistically broad → decrease Wmin.

## Common Adjustment Patterns

| Problem | Primary Fix | Secondary Fix |
|---------|-------------|---------------|
| Peak too high | Decrease binfilt | Increase soil_d2, increase Wc |
| Peak too low | Increase binfilt | Decrease soil_d2, decrease Wc |
| Low flow too low | Increase Ds or Dsmax | Increase soil_d3, increase Wmin |
| Low flow too high | Decrease Ds or Dsmax | Decrease soil_d3 |
| PBIAS > 0 (over) | Increase soil_d2/d3 | Decrease binfilt |
| PBIAS < 0 (under) | Decrease soil_d2/d3 | Increase binfilt |
| Peaks arrive too early | Increase Hc | Increase Hmin |
| Peaks arrive too late | Decrease Hc | Decrease Hmin |
| Peaks too sharp | Increase Wc | Increase Hc |
| Peaks too smooth | Decrease Wc | Decrease Hc |
| Recession too fast (k_ratio > 1) | Increase soil_d3 | Decrease Ds |
| Recession too slow (k_ratio < 1) | Decrease soil_d3 | Increase Ds |
