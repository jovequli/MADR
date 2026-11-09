# I4: MADR without supplied parameter-mechanism knowledge

Remove the separately supplied physical interpretations, conditional expert hypotheses and expert priorities from both roles.

Keep metric definitions, parameter names and bounds, executable constraints, role tasks and output contracts. Keep code-generated hints. This does not remove the model's pretrained knowledge. Preserve generic evidence limitations, missing-data cautions, and definitions used to interpret measured diagnostics; these are not parameter-mechanism priors.

The replacement input fragments in [projection.md](projection.md) retain metric definitions and execution requirements.

## Evaluation budget

Counts are per sub-basin. Initialization and final confirmation are included in the main-loop budget.

| Exploration | Agent rounds | Single-candidate rounds | MC rounds | Main evaluations | Base total |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 50 | 90 | 75 | 15 x 3 | 120 | 170 |

Phase lengths: 50/30/10; MC rounds by phase: 10/5/0. Center rechecks and fixed-flux preparations are reported separately when applicable. Reused exploration counts toward the design budget, not newly executed model calls.

## Run

Set the server paths and optional external credential-file path in `../../MADR/configs/settings.json`. Copy [`../examples/MADR_ablation_example.json`](../examples/MADR_ablation_example.json), set `"arm": "I4"` and `"budget_mode": "matched_reference"`, and give it a new `run_name`, or generate the launch file with `make_batch.py`. Then run from the repository root:

```sh
python -B ablations/run_ablation.py --config <launch file> --action validate
python -B ablations/run_ablation.py --config <launch file> --action run
```

The example uses fresh exploration. A compatible authenticated exploration bundle may be reused with a paired reference run. For each independent repetition, use a distinct `run_name` in the launch file and distinct `random_seed` and `initial_exploration.seed` values in the configuration file named by `team_config`, or generate the repetitions with `make_batch.py`. The ordinary MADR and SADR launchers do not activate this intervention.
