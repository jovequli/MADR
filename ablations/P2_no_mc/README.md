# P2: MADR without multi-candidate search

Use one proposal per station search round instead of MC.

Both mc_rounds and mc_candidates are zero in every phase. The reference candidate-evaluation budget is matched by adjusting the round counts.

Single-candidate rounds replace MC slots, preserving the per-phase candidate-evaluation budgets.

## Evaluation budget

Counts are per sub-basin. Initialization and final confirmation are included in the main-loop budget.

| Exploration | Agent rounds | Single-candidate rounds | MC rounds | Main evaluations | Base total |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 50 | 120 | 120 | 0 x 0 | 120 | 170 |

Phase lengths: 70/40/10; MC rounds by phase: 0/0/0. Center rechecks and fixed-flux preparations are reported separately when applicable. Reused exploration counts toward the design budget, not newly executed model calls.

## Run

Set the server paths and optional external credential-file path in `../../MADR/configs/settings.json`. Copy [`../examples/MADR_ablation_example.json`](../examples/MADR_ablation_example.json), set `"arm": "P2"` and `"budget_mode": "matched_reference"`, and give it a new `run_name`, or generate the launch file with `make_batch.py`. Then run from the repository root:

```sh
python -B ablations/run_ablation.py --config <launch file> --action validate
python -B ablations/run_ablation.py --config <launch file> --action run
```

The example uses fresh exploration. A compatible authenticated exploration bundle may be reused with a paired reference run. For each independent repetition, use a distinct `run_name` in the launch file and distinct `random_seed` and `initial_exploration.seed` values in the configuration file named by `team_config`, or generate the repetitions with `make_batch.py`. The ordinary MADR and SADR launchers do not activate this intervention.
