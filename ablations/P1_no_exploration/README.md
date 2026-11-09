# P1: MADR without initial exploration

Skip initial exploration and provide no exploration summaries to either role.

The reference main-loop phases, MC slots, round count, initial center, final confirmation and recovery are unchanged. The exploration evaluations are not reallocated, and no exploration bundle or cached summary is accepted.

Initial exploration is skipped, and neither role receives an exploration summary. The main-loop schedule is unchanged; the removed 50 evaluations are not reallocated.

## Evaluation budget

Counts are per sub-basin. Initialization and final confirmation are included in the main-loop budget.

| Exploration | Agent rounds | Single-candidate rounds | MC rounds | Main evaluations | Base total |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 90 | 75 | 15 x 3 | 120 | 120 |

Phase lengths: 50/30/10; MC rounds by phase: 10/5/0. Center rechecks and fixed-flux preparations are reported separately when applicable.

## Run

Set the server paths and optional external credential-file path in `../../MADR/configs/settings.json`. Copy [`../examples/MADR_ablation_example.json`](../examples/MADR_ablation_example.json), set `"arm": "P1"` and `"budget_mode": "preserve_main_schedule"`, and give it a new `run_name`, or generate the launch file with `make_batch.py`. Then run from the repository root:

```sh
python -B ablations/run_ablation.py --config <launch file> --action validate
python -B ablations/run_ablation.py --config <launch file> --action run
```

No exploration bundle is accepted. For each independent repetition, use a distinct `run_name` in the launch file and a distinct `random_seed` in the configuration file named by `team_config`, or generate the repetitions with `make_batch.py`. The ordinary MADR and SADR launchers do not activate this intervention.
