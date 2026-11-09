# SADR

SADR keeps the LLM Leader of MADR and replaces each Worker LLM with a rule-based executor. It runs on the shared code in [MADR/](../MADR/): the Leader, metrics, physical execution, phase schedule, acceptance test, recovery handling, and result selection are the same as in MADR. Only the Worker proposals differ. The configuration file selects the method with `"method": "SADR"`, and SADR makes no Worker LLM calls.

## Files

| Part | Location |
| --- | --- |
| Rule-based executor (SADR Worker) | [MADR/scripts/rule_worker_strategy.py](../MADR/scripts/rule_worker_strategy.py) |
| Steps from Leader guidance and escape perturbations | [MADR/scripts/worker_state.py](../MADR/scripts/worker_state.py) |
| Configuration | [MADR/configs/SADR_90round.json](../MADR/configs/SADR_90round.json) |
| Launch file | [MADR/configs/SADR_run.json](../MADR/configs/SADR_run.json) |
| SADR ablations | [SADR-E](../ablations/SADR-E_no_escape/README.md), [SADR-D](../ablations/SADR-D_no_decay/README.md), [SADR-ED](../ablations/SADR-ED_no_escape_no_decay/README.md) |

## Executor

- **Guidance to steps.** For each parameter of the active phase, the Leader's direction sets the sign of the step and its magnitude sets the size: `small`, `medium`, and `large` move the parameter by 10%, 22%, and 40% of its range, multiplied by the step-decay factor, plus Gaussian noise with a standard deviation of 1% of the range. Active parameters that the Leader holds or does not address receive Gaussian noise of 0.5% of the range; parameters outside the active phase stay at the center. When the guidance contains no parameter adjustments, the candidate is the current center.
- **Step decay.** Within each phase, the step factor is `max(floor, start × 0.95^r)` for local round `r`, with `(start, floor)` = (1.00, 0.05) for VIC-only, (0.40, 0.30) for routing-only, and (0.15, 0.10) for joint calibration.
- **Multi-candidate rounds.** The three candidates use step multipliers 0.3, 0.7, and 1.0 (labeled conservative, moderate, and normal). The candidate with the highest NSE goes to the acceptance test.
- **Escape.** A round counts as an improvement when the candidate NSE exceeds the NSE of the current center, evaluated under the same upstream parameters, by more than 0.001. After ten evaluated rounds without improvement, the executor stops following the guidance and perturbs randomly chosen active parameters uniformly within a fraction of their range: three parameters within ±10% (tier 1), four within ±15% (tier 2), and then all active parameters within ±20%, plus 1% for each further round (tier 3). Tiers 1 and 2 last five rounds each. If a tier found an improvement, the executor returns to the guidance; otherwise it moves to the next tier. Tier 3 lasts until the first improvement.

## Run

SADR uses the same requirements, input binding, and [settings](../MADR/configs/settings.json) as MADR (see the main [README](../README.md#quick-start)). Only the Leader calls an LLM, so only the key for the Leader's API profile is needed. From the `MADR/` directory:

```bash
python -B tools/run_madr.py --config configs/SADR_run.json --action validate
python -B tools/run_madr.py --config configs/SADR_run.json --action prepare
python -B tools/run_madr.py --config configs/SADR_run.json --action run
python -B tools/run_madr.py --config configs/SADR_run.json --action summarize
```

The budget is the same as for MADR: 50 exploration evaluations plus 90 main-loop rounds (120 main-loop evaluations) per station. Outputs follow the MADR layout under `settings.work_root/run_name/runs/run_name/`.

The SADR ablations (without escape, without step decay, and without both) start from [ablations/examples/SADR_ablation_example.json](../ablations/examples/SADR_ablation_example.json); see the [ablation catalog](../ablations/README.md).
