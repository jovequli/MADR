# Knowledge-free input fragments

Only the following replacement fragments are supplied by this arm. The shared
runtime continues to supply bounds, dates, identities, schedules, code-generated
diagnostic hints and schema-derived output contracts. These fragments do not
remove the language model's pretrained knowledge.

<!-- BEGIN STATIC: parameter_units -->
## Parameter identifiers and units
binfilt, Ds and Ws are dimensionless; Dsmax uses mm/day; soil_d2 and soil_d3 use m; velocity uses m/s; diffusivity uses m²/s. Use the supplied bounds and execution constraints.
<!-- END STATIC: parameter_units -->

<!-- BEGIN STATIC: leader_evidence_limits -->
Do not infer climate, preferred parameter intervals or shared optima from basin names. Do not invent unreported soil moisture, recharge, ET or internal baseflow.
<!-- END STATIC: leader_evidence_limits -->

<!-- BEGIN STATIC: metric_definitions -->
## Interpret the supplied diagnostics
Positive PBIAS means excess simulated volume over the scored dates. peak_ratio and lowflow_ratio are sim/obs mean-flow ratios on observed high-flow and low-flow days, not matched-event peak heights. Ratios above/below one indicate excess/deficit for that statistic only.

peak_timing_bias_days is simulated minus observed matched peak date: negative is early, positive late.

peak_magnitude_bias_pct concerns matched-event height.

recession_k_ratio compares mean absolute log-recession slopes, sim/obs: above one is faster simulated decay, below one slower; it is not a recession-duration ratio.

baseflow_index_bias_pct compares filtered baseflow fractions, not absolute flow or VIC internal baseflow. Check low-flow and total-flow evidence because the fraction also depends on its denominator and hydrograph shape. Missing or failed metrics are unavailable, not zero.
<!-- END STATIC: metric_definitions -->
