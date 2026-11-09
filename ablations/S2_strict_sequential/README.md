# S2: MADR with strict station-by-station calibration

Complete fresh exploration and the entire local calibration schedule for each station, permanently freeze its confirmed effective parameters, then continue downstream.

Preserve each station local phases, candidate counts, diagnostics, knowledge, schema, acceptance, numerical constraints and recovery. Recursive upstream inputs remain physically frozen. Local NSE replaces unavailable global feedback; downstream exploration starts after upstream freezing. These are intrinsic workflow differences.

The schedule is completed separately for each station in topological order. Each station performs fresh exploration under its permanently frozen upstream parameters; there are no downstream center rechecks. The 90 rounds are local agent rounds, not 170 agent rounds.

## Evaluation budget

Counts are per sub-basin. Initialization and final confirmation are included in the main-loop budget.

| Exploration | Agent rounds | Single-candidate rounds | MC rounds | Main evaluations | Base total |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 50 | 90 | 75 | 15 x 3 | 120 | 170 |

Phase lengths: 50/30/10; MC rounds by phase: 10/5/0. Center rechecks and fixed-flux preparations are reported separately when applicable. Reused exploration counts toward the design budget, not newly executed model calls.

## Run

Set the server paths and optional external credential-file path in `../../MADR/configs/settings.json`. Copy [`../examples/MADR_ablation_example.json`](../examples/MADR_ablation_example.json), set `"arm": "S2"` and `"budget_mode": "matched_reference"`, and give it a new `run_name`, or generate the launch file with `make_batch.py`. Then run from the repository root:

```sh
python -B ablations/run_ablation.py --config <launch file> --action validate
python -B ablations/run_ablation.py --config <launch file> --action run
```

Fresh exploration is required for each station. For each independent repetition, use a distinct `run_name` in the launch file and distinct `random_seed` and `initial_exploration.seed` values in the configuration file named by `team_config`, or generate the repetitions with `make_batch.py`. The ordinary MADR and SADR launchers do not activate this intervention.
