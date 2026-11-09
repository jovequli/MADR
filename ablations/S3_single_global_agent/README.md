# S3: single global agent with a minimal execution harness

This external comparison replaces MADR's organization as a whole. One global LLM
proposes all eight parameters for every configured station in one JSON response.
Every proposed vector is evaluated at every station in topological order. The
representative result is the complete evaluated vector with the highest arithmetic
mean evaluation-period NSE; ties retain the earliest vector. Station-wise best
values from different evaluations are never assembled into an unevaluated result.

## Protocol

The default budget is **170 full-vector evaluations**, or **170 evaluations per
station**. Evaluation 1 uses the same configured default parameters as MADR.
Evaluations 2–170 use 169 global-agent proposals. A five-station run therefore has
850 model-evaluation requests. There is no extra final confirmation evaluation.
Failed physical evaluations consume their request slots and cannot improve the
representative result. There is no early stopping based on NSE.

The agent receives parameter definitions, executable bounds and precision, dates,
topology, the best complete vector, and the five most recent evaluated vectors
with their measured metrics and effective parameters. It does not receive
exploration summaries, code-generated diagnostic recommendations, symptom-to-
parameter rules, a Leader/Worker role hierarchy, phase scheduling, multi-candidate
strategies, anti-flip filtering, or a composite diagnostic objective. All eight
parameters remain adjustable. The two static Markdown files and the executable
JSON response schema in this directory are the entire S3 prompt source.

The recent-history window is explicit in `run.json`; the best complete vector is
retained independently. All evaluated vectors remain on disk. The configured seed
identifies repetitions; this harness has no random code proposal or local random
search and does not force a provider seed unsupported by the shared API profile.

For nested stations, each full-vector evaluation uses upstream parameter values
from that same vector. Upstream ownership masks are composed in the same
deepest-first order as the shared executor, so more distant headwater ownership
overrides overlapping intermediate masks. No upstream parameter is permanently
frozen and no within-vector local acceptance mixes two proposed vectors.

## Shared code and limits of the comparison

S3 imports the common `VICExecutor`, input isolation, topology validation,
parameter normalization, physical model runner, date/scoring protection, API
client, API retry settings, network routing policy and token-attempt accounting.
It does not import the MADR graph, Leader, Worker or diagnostic strategy.
Baseline files are read only; S3 does not patch or monkey-patch them.

API attempts use the same capped retry and timeout settings as the selected
baseline physical configuration. After retries are exhausted, S3 explicitly
repeats its best complete vector, or the default when no valid vector exists,
then attempts the LLM normally at the next scheduled call. This neutral recovery
does not introduce MADR's diagnostic fallback or a new optimizer. It consumes an
evaluation slot, records the exhausted attempts and marks the run as degraded.
Missing credentials, invalid configuration and unexpected code errors stop the
run rather than generating synthetic responses. Usage without a provider report
is recorded as unknown, not free. Degraded runs require separate review before
being used in a scientific comparison.

The comparison matches the **base physical-evaluation budget**, not the number of
LLM calls, token budget, search dimension, or initialization strategy. MADR's
additional center checks are reported separately. S3 tests the complete organized
framework against a simple global agent; it cannot isolate the causal effect of
the number of agents alone.

## Linux entry

Use the repository's existing `MADR/configs/settings.json` and input-binding format.
Set external Linux input paths, work directory, and optional API key-file path in
that settings file. Credentials must remain outside the package and run directory.
The `physical_config` in `run.json` supplies only station topology, physical input
metadata, initial parameters, bounds, evaluation dates and the shared API failure
policy. Its MADR prompts, schedule and exploratory state are not loaded into S3.

From the repository root:

```bash
python -B ablations/S3_single_global_agent/run.py validate
python -B ablations/S3_single_global_agent/run.py prepare
python -B ablations/S3_single_global_agent/run.py run
```

`validate` reads configuration and reports the budget, without API/model calls.
`prepare` isolates and fingerprints physical inputs and freezes a source snapshot,
also without API/model calls. `run` uses that frozen snapshot. If needed it prepares
the run first. Real execution requires Linux and passes the same pinned Python,
dependency, ELF architecture and shared-library checks as the standard launcher.
A run lock prevents concurrent use of one work directory. Existing run directories
are not overwritten. An interrupted run is
retained for audit; restart under a fresh `run_name` rather than appending duplicate
evaluations to it. This entry does not launch other comparisons.

Supported API profiles are the same as the standard launcher: `ds_default`,
`sonnet_default`, `sonnet_cached`, and `gemini_default`. DeepSeek bypasses proxy
environment variables in its child process; Sonnet/Gemini use the proxy settings of
the environment when present. Provider thinking settings remain those of the common profile.
Independent repetitions need unique `run_name` values and separate work directories.

For an interface-only short test, lower `global_evaluations` in a copy of `run.json`.
Such a run is not the 170-evaluation S3 comparison. Validation and preparation make no API or physical-model calls.

## Retained evidence

- `manifest.json`: frozen source, resolved configuration and input fingerprints;
  fingerprints bind that run's own inputs, not a hard-coded catchment.
- `results/system_prompt.txt` and `effective_protocol.json`: exact static input
  and the effective protocol.
- `results/evaluation_NNN/`: submitted/effective full vector, request, accepted
  response when available, station logs, complete measured results and failures.
- `results/llm_calls.jsonl`: all API attempts, including retry responses, raw
  provider usage and reported reasoning where available.
- `results/llm_recoveries.jsonl`: exhausted calls and neutral fallback evaluations.
- `results/native_audit/`: shared actual VIC/routing process counters and retained
  small native input/output evidence.
- `results/checkpoint.json`: latest complete accounting position and coherent best.
- `results/final_summary.json`: representative vector, request/native-call counts,
  usage completeness, failures and degraded status.

The output-only schema file is specialized with the configured station IDs and
all eight parameter fields. The same specialized contract is included in the
agent input and validated before any proposed vector reaches the executor.
The snapshot also retains common schema/static-prompt dependencies used by shared
client utilities; retaining these files does not inject MADR knowledge into S3.
