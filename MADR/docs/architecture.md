# MADR multi-model architecture

MADR separates hydrological execution, diagnostic knowledge, and LLM transport
behind narrow interfaces. The same LangGraph workflow therefore supports
multiple model combinations without treating them as physically equivalent.

## Model combinations

| Combination | Execution path | Phases | Parameters |
|---|---|---|---:|
| `vic_routing` | VIC 5.1 + Lohmann routing | `VIC_ONLY → ROUTING_ONLY → JOINT` | 8 |
| `vic_cama_flood` | VIC 5.1 + CaMa-Flood 4.20 | `VIC_ONLY → CAMA_FLOOD_ONLY → JOINT` | 10 |
| `swat` | SWAT+ integrated land and routing | `SWATPLUS_HRU` | 18 |

Each combination provides:

- a `ModelExecutor` implementation under `scripts/executors/`;
- six model-specific prompt and knowledge files under `prompts/<combination>/`;
- combination-aware parameter ranges and phase locks;
- a public example configuration under `configs/`.

Adding a hydrological model requires implementing and physically validating all
four pieces. This is intentionally more demanding than adding an LLM API
adapter, which is normally a configuration or transport-layer change.

## Shared graph

The graph performs the following sequence:

1. Validate configuration, topology, paths, parameter ranges, and periods.
2. Execute Round 1 at the configured default center.
3. Activate Workers by topological level, except for SWAT+ joint execution.
4. Run the selected hydrological combination and compute comparable metrics.
5. Diagnose volume, peak, low-flow, timing, and recession behavior when those
   signatures are meaningful at the observation timestep.
6. Ask the Leader for structured cross-basin guidance.
7. Update centers and patience state.
8. Run one mandatory force-best final evaluation outside the optimization
   budget.

Missing or non-finite station NSE values invalidate the global round instead of
silently dropping failed stations from the mean.

## Round budgets

Each phase-level `max_rounds` counts optimization rounds. The optional
top-level `max_rounds` overrides the sum of those phase caps; the CLI
`--max-rounds` override has the highest precedence. Regardless of which source
sets the optimization cap, the runtime reserves one additional force-best
verification round. Patience may end the pipeline earlier, but the final
verification still runs once.

The first global round is always the single-candidate default-parameter
baseline. Consequently, an `mc_rounds` window in the first phase includes
Round 1 numerically but activates multi-candidate generation only from Round 2.
Later phases use multi-candidate generation throughout their configured
windows.

## VIC-based topological execution

For `vic_routing` and `vic_cama_flood`, `neighbors_downstream` defines a
directed acyclic graph. Configuration validation rejects unknown downstream
identifiers, cycles, and invalid outlets. Workers at the same topological level
may run in parallel; downstream workers run after their upstream dependencies.

Downstream executions inherit already-selected upstream land parameters.
Lohmann routing grids splice upstream routing parameters by geographic cell.
CaMa-Flood channel parameters use per-reach ownership with upstream locking.

The two VIC-based combinations keep per-basin historical best centers. Phase
transitions use basin-average NSE plateau detection outside the multi-candidate
window.

## SWAT+ joint execution

SWAT+ routes the complete watershed internally, so independently simulating
each Worker would not represent one physically consistent parameter field.
Workers still propose parameters independently, but execution is joint:

1. Convert configured geographic HRU identifiers to SWAT+ unit identifiers.
2. Build mutually exclusive HRU sets from the nested-gauge topology.
3. Write one `calibration.cal` containing every Worker's parameters.
4. Run SWAT+ once per strategy index.
5. Compute metrics for every gauge from that same watershed run.
6. Select one basin-wide strategy index using `mc_selection_metric`.

Across rounds, SWAT+ stores an atomic watershed-level best bundle containing
the strategy index, all Worker parameters, all station metrics, mean NSE, and
J_total. A candidate bundle is accepted or rejected as a whole. Centers,
patience, probe baselines, Worker prompt anchors, and the final force-best run
all reference this bundle, preventing parameters from different unverified
joint simulations from being mixed.

## Simulation and evaluation periods

`simulation_period` controls the model run. Optional `evaluation_period`
controls only the dates entering metrics and Agent feedback. This makes spin-up
explicit—for example, simulate 1980–1990 and evaluate 1981–1990.

Both year-form and ISO-date-form evaluation windows are accepted. The same
semantics are propagated through VIC+Lohmann, VIC+CaMa-Flood, and SWAT+.

Daily simulations are aggregated to monthly means when observations are
monthly. Daily-only signatures such as peak timing in days, event recession,
and daily-filter baseflow index are reported as unavailable at monthly scale
rather than being interpreted with incorrect units.

## Objectives and reporting

The global objective is:

```text
J_total = w_mean × mean(NSE) - w_consistency × consistency_penalty
```

The consistency term uses peak- and low-flow ratios when available. Both
J_total and NSE are persisted. A run may optimize the richer joint objective
while reporting station-wise and basin-average NSE as the principal performance
summary.

## LLM transport

The API build includes tested adapters for several native and compatible API
families. The list is illustrative rather than exhaustive: another
API-accessible model can normally be connected through provider configuration,
a compatible base URL, or a thin adapter in `llm_client.py`.

Leader responses are checked against a recursive JSON schema. Model-specific
parameter names are then validated against the active run configuration before
guidance reaches Workers.

## Portability and privacy

- Paths come from configuration or environment variables.
- Public configurations contain only `/path/to/...` placeholders.
- The optional SWAT+ `cal_parms.cal` fallback is supplied through
  `MADR_SWATPLUS_CAL_PARMS`.
- No local provider choice, API credential, output directory, or checkpoint is
  required by the source tree.
- Subprocess calls use explicit working directories and portable Python APIs.
