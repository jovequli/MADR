Round {round_id} | J={j_total:.5f} | outlet_NSE={outlet_nse:.4f} | mean_NSE={mean_sub_nse:.4f}

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
      "subbasin_id": "xixian",
      "priority_focus": "fix dry bias",
      "search_radius_hint": "keep",
      "param_adjustments": [
        {{"param": "binfilt", "direction": "increase", "magnitude": "medium"}},
        {{"param": "Ds", "direction": "hold", "magnitude": "none"}}
      ]
    }}
  ],
  "next_round_strategy": "Increase binfilt in headwaters to fix dry bias."
}}
```
Include ALL basins in worker_guidance. Output ONLY the JSON object.