# H2: No exploration with 50 replacement online evaluations

Remove exploration and both roles' exploration summaries, and allocate 50 additional single-candidate evaluations to the VIC phase.

Use 100/30/10 main-loop rounds and the baseline 10/5 MC rounds. The default center remains unchanged. This is a matched base-evaluation-budget substitution, with more LLM interactions than the baseline.

Initial exploration is skipped. Its 50 evaluations are reassigned to additional single-candidate VIC rounds, preserving 170 base evaluations per station.

## Evaluation budget

Counts are per sub-basin. Initialization and final confirmation are included in the main-loop budget.

| Exploration | Agent rounds | Single-candidate rounds | MC rounds | Main evaluations | Base total |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 140 | 125 | 15 x 3 | 170 | 170 |

Phase lengths: 100/30/10; MC rounds by phase: 10/5/0. Center rechecks and fixed-flux preparations are reported separately when applicable.

## Run

Set the server paths and optional external credential-file path in `../../MADR/configs/settings.json`. Copy [`../examples/MADR_ablation_example.json`](../examples/MADR_ablation_example.json), set `"arm": "H2"` and `"budget_mode": "sensitivity_schedule"`, and give it a new `run_name`, or generate the launch file with `make_batch.py`. Then run from the repository root:

```sh
python -B ablations/run_ablation.py --config <launch file> --action validate
python -B ablations/run_ablation.py --config <launch file> --action run
```

No exploration bundle is accepted. For each independent repetition, use a distinct `run_name` in the launch file and a distinct `random_seed` in the configuration file named by `team_config`, or generate the repetitions with `make_batch.py`. The ordinary MADR and SADR launchers do not activate this intervention.
