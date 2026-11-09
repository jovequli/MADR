# H4: Phase lengths 60/20/10

Use VIC/routing/JOINT phase lengths of 60/20/10, retaining 10/5 MC rounds.

Keep 90 main-loop rounds, 120 main evaluations and 50 exploration samples. Move phase boundaries only; retain phase-specific parameter locks and prompts.

## Evaluation budget

Counts are per sub-basin. Initialization and final confirmation are included in the main-loop budget.

| Exploration | Agent rounds | Single-candidate rounds | MC rounds | Main evaluations | Base total |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 50 | 90 | 75 | 15 x 3 | 120 | 170 |

Phase lengths: 60/20/10; MC rounds by phase: 10/5/0. Center rechecks and fixed-flux preparations are reported separately when applicable. Reused exploration counts toward the design budget, not newly executed model calls.

## Run

Set the server paths and optional external credential-file path in `../../MADR/configs/settings.json`. Copy [`../examples/MADR_ablation_example.json`](../examples/MADR_ablation_example.json), set `"arm": "H4"` and `"budget_mode": "sensitivity_schedule"`, and give it a new `run_name`, or generate the launch file with `make_batch.py`. Then run from the repository root:

```sh
python -B ablations/run_ablation.py --config <launch file> --action validate
python -B ablations/run_ablation.py --config <launch file> --action run
```

The example uses fresh exploration. A compatible authenticated exploration bundle may be reused with a paired reference run. For each independent repetition, use a distinct `run_name` in the launch file and distinct `random_seed` and `initial_exploration.seed` values in the configuration file named by `team_config`, or generate the repetitions with `make_batch.py`. The ordinary MADR and SADR launchers do not activate this intervention.
