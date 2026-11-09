# I2: MADR without J_total feedback

Remove J_total from model-facing current feedback, historical score columns, runtime formula/weights and code recovery assessments; do not introduce a replacement aggregate.

Retain all existing individual metrics, existing outlet/mean NSE fields, diagnostic information, history round coverage and station-detail window. Shared numerical objectives, archives, local acceptance, recovery actions and final representative selection are unchanged.

Tests removal of the supplied composite J_total feedback and its all-round trajectory; mean and outlet NSE remain visible.

## Evaluation budget

Counts are per sub-basin. Initialization and final confirmation are included in the main-loop budget.

| Exploration | Agent rounds | Single-candidate rounds | MC rounds | Main evaluations | Base total |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 50 | 90 | 75 | 15 x 3 | 120 | 170 |

Phase lengths: 50/30/10; MC rounds by phase: 10/5/0. Center rechecks and fixed-flux preparations are reported separately when applicable. Reused exploration counts toward the design budget, not newly executed model calls.

## Run

Set the server paths and optional external credential-file path in `../../MADR/configs/settings.json`. Copy [`../examples/MADR_ablation_example.json`](../examples/MADR_ablation_example.json), set `"arm": "I2"` and `"budget_mode": "matched_reference"`, and give it a new `run_name`, or generate the launch file with `make_batch.py`. Then run from the repository root:

```sh
python -B ablations/run_ablation.py --config <launch file> --action validate
python -B ablations/run_ablation.py --config <launch file> --action run
```

The example uses fresh exploration. A compatible authenticated exploration bundle may be reused with a paired reference run. For each independent repetition, use a distinct `run_name` in the launch file and distinct `random_seed` and `initial_exploration.seed` values in the configuration file named by `team_config`, or generate the repetitions with `make_batch.py`. The ordinary MADR and SADR launchers do not activate this intervention.
