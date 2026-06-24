Round {round_id} | J={j_total:.5f} | outlet_NSE={outlet_nse:.4f} | mean_NSE={mean_sub_nse:.4f}

Model: SWAT+ with integrated routing. Calibration is via calibration.cal
with change-types absval/abschg/pctchg. The 13 core tunable params are:
cn2, esco, awc, surlag, lat_ttime, canmx, epco, perco, alpha,
revap_co, revap_min, flo_min, deep_seep.
Plus 5 snow params for Tangnaihai:
snofall_tmp, snomelt_tmp, snomelt_max, snomelt_min, snomelt_lag.
NOTE: `gw_delay` and `alpha_bf` are DEAD names in SWAT+ — do NOT propose them.

{worker_results_block}

## Full History
{history_table}

You MUST respond with a single JSON object. Example:
```json
{{
  "overall_assessment": "Headwaters show dry bias, outlet wet bias.",
  "global_score_trend": "improving",
  "stop_or_continue": "continue",
  "phase_recommendation": "stay",
  "worker_guidance": [
    {{
      "subbasin_id": "<subbasin_a>",
      "priority_focus": "fix dry bias",
      "search_radius_hint": "keep",
      "param_adjustments": [
        {{"param": "cn2", "direction": "increase", "magnitude": "medium"}},
        {{"param": "alpha", "direction": "hold", "magnitude": "none"}}
      ]
    }}
  ],
  "next_round_strategy": "Increase cn2 in headwaters to fix dry bias."
}}
```
Include ALL basins in worker_guidance. Output ONLY the JSON object.
