# Ablation experiments

This directory provides 20 nonbaseline configurations: 12 structural, information and procedure ablations, five budget and phase configurations, and three SADR auxiliary controls. MADR and SADR references use the ordinary launchers and are not counted as ablation groups.

The internal configurations share the MADR/SADR runtime and change only the specified intervention and its required schedule or information scope. S3 is a separate complete-framework comparison. Ordinary runs do not activate these adapters.

## Main ablations

| Category | ID | Configuration |
| --- | --- | --- |
| Structure | S1 | [MADR without the global Leader](S1_no_leader/README.md) |
| Structure | S2 | [MADR with strict station-by-station calibration](S2_strict_sequential/README.md) |
| Structure | S3 | [Calibration with a single global agent and a minimal execution harness](S3_single_global_agent/README.md) |
| Information | I1 | [MADR without explicit upstream-update messages](I1_no_upstream_updates/README.md) |
| Information | I2 | [MADR without J_total feedback](I2_no_j_feedback/README.md) |
| Information | I3 | [MADR without code-generated diagnostic advice](I3_no_code_advice/README.md) |
| Information | I4 | [MADR without supplied parameter-mechanism knowledge](I4_no_expert_knowledge/README.md) |
| Information | I5 | [MADR without anti-flip filtering](I5_no_anti_flip/README.md) |
| Procedure | P1 | [MADR without initial exploration](P1_no_exploration/README.md) |
| Procedure | P2 | [MADR without multi-candidate search](P2_no_mc/README.md) |
| Procedure | P3 | [MADR without phase separation](P3_no_phases/README.md) |
| Procedure | P4 | [MADR without multi-candidate search and phase separation](P4_no_mc_no_phases/README.md) |

## Budget and phase sensitivity

| ID | Configuration | Exploration | Phase rounds | Main evaluations | Base total |
| --- | --- | ---: | --- | ---: | ---: |
| H1 | [Long-budget calibration](H1_extended_budget/README.md) | 50 | 100/60/20 | 240 | 290 |
| H2 | [No exploration with 50 replacement online evaluations](H2_no_exploration_matched/README.md) | 0 | 100/30/10 | 170 | 170 |
| H3 | [Phase lengths 40/40/10](H3_phase40_40_10/README.md) | 50 | 40/40/10 | 120 | 170 |
| H4 | [Phase lengths 60/20/10](H4_phase60_20_10/README.md) | 50 | 60/20/10 | 120 | 170 |
| H5 | [Phase lengths 45/25/20](H5_phase45_25_20/README.md) | 50 | 45/25/20 | 120 | 170 |

## SADR auxiliary mechanisms

These controls use SADR as their reference. Their SADR-specific IDs are separate from the MADR procedure group P1–P4.

| ID | Configuration |
| --- | --- |
| SADR-E | [SADR without escape](SADR-E_no_escape/README.md) |
| SADR-D | [SADR without step decay](SADR-D_no_decay/README.md) |
| SADR-ED | [SADR without escape and step decay](SADR-ED_no_escape_no_decay/README.md) |

## Budgets and inputs

The standard per-station budget is 50 exploration evaluations plus 120 main evaluations: 75 single-candidate rounds and 15 three-candidate rounds, totaling 90 agent rounds. Center rechecks and fixed-flux preparations are recorded separately. P1 omits the 50 exploration evaluations. P2 and P4 preserve 120 main evaluations using 120 single-candidate rounds.

S2 completes fresh exploration and all local phases for one station before freezing its parameters and moving downstream. Its 90 agent rounds and 170 base evaluations are per station. S3 instead uses 170 complete-network evaluations: one default vector and 169 global-agent proposals.

A compatible authenticated exploration bundle may be reused for paired comparisons, except S2 requires fresh local exploration and P1/H2 omit exploration entirely. Each independent repetition needs a distinct run name and seeds. Parameter bounds, precision, physical inputs, scoring windows and failure accounting remain shared unless explicitly part of the comparison.

## Run an internal ablation

Configure `../MADR/configs/settings.json`. Two launch examples are provided in [examples/](examples/): one for an MADR arm (I3) and one for a SADR arm (SADR-E). Launch files of different arms differ only in `ablation.arm`, `ablation.budget_mode`, `team_config` and `run_name`. Copy the example for the arm's method, set the arm ID and the budget mode from the table below, and give the copy a new `run_name`.

| Method | Arms | Example to copy | `budget_mode` |
| --- | --- | --- | --- |
| MADR | S1, S2, I1, I2, I3, I4, I5, P2, P3, P4 | [`MADR_ablation_example.json`](examples/MADR_ablation_example.json) | `matched_reference` |
| MADR | P1 | [`MADR_ablation_example.json`](examples/MADR_ablation_example.json) | `preserve_main_schedule` |
| MADR | H1, H2, H3, H4, H5 | [`MADR_ablation_example.json`](examples/MADR_ablation_example.json) | `sensitivity_schedule` |
| SADR | SADR-D, SADR-E, SADR-ED | [`SADR_ablation_example.json`](examples/SADR_ablation_example.json) | `matched_reference` |

Then run from the repository root:

```sh
python -B ablations/run_ablation.py --config ablations/examples/MADR_ablation_example.json --action validate
python -B ablations/run_ablation.py --config ablations/examples/MADR_ablation_example.json --action run
```

`prepare` and `summarize` actions are also available. Each arm directory contains its specification and README. S3 has its own [entry point](S3_single_global_agent/README.md). The [catalog](catalog.json) and [protocol record](PROTOCOL_VERSIONS.md) define the release identifiers.

## Independent repetitions

`make_batch.py` creates configurations without starting any computation. For example:

```sh
python -B ablations/make_batch.py --settings MADR/configs/settings.json \
  --output ../ablation_batches/rep5 --arms all-internal --provider sonnet_cached \
  --repetitions 5 --seed-start 1001 --max-parallel 8 --include-controls
```

This creates 19 internal configurations and the MADR/SADR references per
repetition. S3 is run separately. To reuse prespecified reference exploration,
supply `--pairing-manifest`: each MADR/SADR reference entry records its repetition,
run ID, main seed, exploration seed, complete bundle path and SHA256. A single
`--exploration-source` can only be used with one repetition. Reference scores are
not imported automatically. See `make_batch.py --help` for the launch options.

A pairing manifest has this form, with one entry per repetition and method. A run
writes its bundle to `<work_root>/<run_name>/runs/<run_name>/exploration_bundle.json`.

```json
{"schema": "madr-ablation-pairing-v1", "provider_profile": "sonnet_cached",
 "reference_sets": {
   "MADR": [{"repetition": 1, "reference_run_id": "madr_ref_rep1",
             "main_random_seed": 1001, "exploration_seed": 1001,
             "exploration_source": "/path/to/exploration_bundle.json",
             "bundle_sha256": "<sha256 of the bundle file>"}],
   "SADR": [...]}}
```

Run the generated suite with `run_suite.py`. `run` prepares any run that is not yet
prepared; `status`, `summarize` and `pack` report on a started suite.

```sh
python -B ablations/run_suite.py --suite ../ablation_batches/rep5/suite.json --action validate
python -B ablations/run_suite.py --suite ../ablation_batches/rep5/suite.json --action run
```
