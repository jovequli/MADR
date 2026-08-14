Round {round_id} | J={j_total:.5f} | outlet_NSE={outlet_nse:.4f} | mean_NSE={mean_sub_nse:.4f}

Model: wflow_sbm with integrated kinematic-wave routing. Calibration edits the
gridded staticmaps. The 8 tunable params are:
KsatHorFrac, KsatVer, f, SoilThickness, N, N_River, TT, Cfmax.
mult params (multiply base map): KsatVer, f, SoilThickness.
absval params (set per-cell value): KsatHorFrac, N, N_River, TT, Cfmax.
KsatHorFrac and KsatVer are reasoned in LOG steps (multiplicative), not linear;
explore their FULL range incl. HIGH values (a peak OVERSHOOT often needs HIGH
KsatHorFrac / KsatVer). TT (snowfall temp threshold, °C) and Cfmax (degree-day
melt coeff) tune the spring snowmelt freshet: raise TT for more snow, lower
Cfmax to delay the melt — critical in snow-dominated (sub-freezing winter)
basins where the freshet can DOMINATE the error.

{worker_results_block}

## Full History
{history_table}

You MUST respond with a single JSON object. Example:
```json
{{
  "overall_assessment": "Baseflow fraction too high, peaks under-predicted.",
  "global_score_trend": "improving",
  "stop_or_continue": "continue",
  "phase_recommendation": "stay",
  "worker_guidance": [
    {{
      "subbasin_id": "<subbasin_id from the worker results above>",
      "priority_focus": "raise peaks / lower baseflow fraction",
      "search_radius_hint": "keep",
      "param_adjustments": [
        {{"param": "KsatHorFrac", "direction": "decrease", "magnitude": "medium"}},
        {{"param": "N_River", "direction": "hold", "magnitude": "none"}}
      ]
    }}
  ],
  "next_round_strategy": "Lower KsatHorFrac to shift water from baseflow to storm response."
}}
```
Use the exact subbasin_id values listed in the worker results above; do not copy this placeholder. Include ALL basins in worker_guidance. Output ONLY the JSON object.
