Round {round_id} | J={j_total:.5f} | outlet_NSE={outlet_nse:.4f} | mean_NSE={mean_sub_nse:.4f}

Model: SWAT+ with integrated routing. Calibration is via calibration.cal
with change-types absval/abschg/pctchg. The 19 tunable params are:
cn2, esco, epco, perco, petco, cn3_swf, ovn, lat_ttime, canmx,
awc, k, alpha, revap_co, revap_min, flo_min, deep_seep, bf_max,
surlag, chn.
pctchg params: cn2, awc, k, ovn, chn, bf_max (the rest are absval).
NOTE: `gw_delay`, `alpha_bf`, and the 5 snow params are DEAD names here — do NOT propose them.

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
      "subbasin_id": "<subbasin_id from the worker results above>",
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
Use the exact subbasin_id values listed in the worker results above; do not copy this placeholder. Include ALL basins in worker_guidance. Output ONLY the JSON object.
