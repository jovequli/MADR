# MADR — Multi-Agent Diagnostic Reasoning for Distributed Hydrological Model Calibration

MADR is an LLM-driven framework that calibrates distributed hydrological models in nested-basin systems. A **Leader Agent** synthesizes cross-station simulation errors into structured per-basin guidance; **Worker Agents** translate that guidance into concrete parameter sets. The framework is implemented in [LangGraph](https://github.com/langchain-ai/langgraph) and treats both the hydrological model and the LLM API as pluggable components. Four hydrological-model combinations and several tested LLM adapters — usable over both **API and local CLI** — are included.

In the paper, MADR approaches the calibration accuracy of two benchmark optimization algorithms (SCE-UA, NSGA-III) at less than 5% of their evaluation budget across five nested gauging stations of the Huai River basin, and Worker-level LLM reasoning consistently outperforms a deterministic rule-based executor across both Gemini and Claude.

📦 **Preprint**: Zhao, Li, Zhu, Jin, Zhang (2026). *LLM-Driven Diagnostic Reasoning for Distributed Hydrological Model Calibration*. Research Square. [10.21203/rs.3.rs-10106384/v1](https://doi.org/10.21203/rs.3.rs-10106384/v1)
📂 **Dataset**: observations, model inputs, all calibration runs, and benchmark outputs on Zenodo. [10.5281/zenodo.20622937](https://doi.org/10.5281/zenodo.20622937)

> **Version note.** The initial public code release associated with the Version 1 preprint is preserved under tag [`v0.1.0`](https://github.com/jovequli/MADR/tree/v0.1.0). Release [`v0.2.0`](https://github.com/jovequli/MADR/tree/v0.2.0) introduced the first correctness and robustness update. Release [`v0.2.1`](https://github.com/jovequli/MADR/tree/v0.2.1) synchronizes the public run instructions, formal example pipelines, dependency installation, and round-budget semantics. Release **v0.3.0** (this integrated build) adds a fourth hydrological model (wflow), CLI access with no API key, and a reproducibility layer — see the Update section below.

---

## 🆕 Update — Release v0.3.0 (integrated build)

This release consolidates the previously separate, individually validated modules into a single integrated build, adds a fourth hydrological model, and adds a reproducibility layer. The Lohmann `routing/` model and the `SADR/` ablation control are unchanged from the previous release.

**New**

- **WFlow (`wflow_sbm`) model combination** — a fourth model driven by the same Leader–Worker workflow (integrated land surface + kinematic-wave routing; 8 tunable parameters including snow). See the model table below.
- **CLI access, no API key** — the Worker/Leader LLM socket now accepts local-CLI providers (`claude_cli`, `codex_cli`) alongside the API adapters, so a run can be driven entirely through an installed CLI command with no API key set. The framework is not tied to any model version: switching the LLM is a different CLI command or a different API key, nothing more.
- **Reproducibility & run management** — every run now writes a provenance snapshot (exact config, prompt tree, code version, environment), a verbatim LLM-call log (`llm_calls.jsonl`), and a convergence plot; a tiered archiver compresses and prunes completed runs by regenerability. See the new section below.
- **Cross-combination consistency** — a single shared streamflow-metrics module (identical NSE/KGE/PBIAS/signatures across all combinations); VIC monthly-observation support and an explicit evaluation window; SWAT+ object-type parameter routing (`hru`/`sol`/`aqu`/`bsn`/`rte`) with HRU→aquifer mapping and a literature-aligned parameter set.

**Fixed**

- **Per-candidate diagnostics are now saved on every iteration.** In multi-candidate rounds, each candidate's diagnostic reasoning is persisted to its own `worker_<basin>_c<k>_<strategy>_diagnostic.json`, so no diagnostic information is lost during the iteration.
- **CLI usage is now supported.** Claude/Codex can be driven through the local CLI with no `ANTHROPIC_API_KEY` required.

## Repository contents

| Folder | Purpose |
|---|---|
| **`MADR/`** | The framework. Four hydrological-model combinations and tested LLM adapters (API and CLI) behind a single LangGraph workflow. |
| **`SADR/`** | Single-Agent Diagnostic Reasoning ablation control — the Leader is retained, but the LLM-based Worker is replaced by a deterministic rule-based executor. Scope: VIC + Lohmann routing only. |
| **`routing/`** | The Lohmann routing model Fortran source with this study's local modifications and a Makefile. Build with `cd routing && make`. |

---

## What the framework does

Three characteristics define MADR:

1. **Three sources of knowledge** are combined at every round — the LLM's pretrained domain knowledge, expert knowledge supplied via prompts and a diagnostic-rules table, and iterative understanding built from per-round simulation feedback.
2. **Dynamic information exchange** runs among the agents both within a round (downstream Workers see upstream Workers' fresh proposals) and across rounds (the Leader's per-basin guidance evolves with the search trajectory).
3. **Phased decomposition** splits calibration into sequential stages over tractable parameter subspaces, so each round of LLM reasoning operates at a lower effective dimensionality.

### Leader / Worker division

Global guidance and per-basin parameter proposals both draw on pretrained knowledge, expert rules, and per-round feedback, but the two reasoning tasks emphasize different aspects. MADR therefore separates them into two coordinated roles:

- **Leader Agent** — global-view agent. Activated once at the end of each round, it integrates the per-basin diagnostic results into a system-wide evaluation, reasons over the upstream–downstream topology, and outputs per-basin direction-and-magnitude guidance for the next round. The Leader does **not** produce parameter values directly.
- **Worker Agents** — N local-view agents, one per sub-basin. Each Worker is activated at the start of each round to translate the Leader's guidance for its basin into a concrete parameter proposal, using its own performance history and diagnostic signals as additional context.

```
                                    ┌─→ Worker 1            ─→ Model run
                                    ├─→ Worker 2            ─→ Model run
Leader (per-round global diagnosis) ┼─→ Worker 3            ─→ Model run
                                    ├─→ ...                 ─→ Model run
                                    └─→ Worker N (outlet)   ─→ Model run
```

A complete round proceeds through four steps:

1. **Worker activation** — Workers are invoked in topological order, headwater first, using the Leader's guidance from the previous round.
2. **Simulation** — Proposed parameters drive the model combination selected for the run.
3. **Evaluation** — Simulated streamflow is scored with overall objectives, statistical metrics (NSE, KGE, PBIAS, RMSE), and process signatures (peak ratio, peak timing bias, recession K ratio, etc.).
4. **Leader review** — The Leader produces guidance for the next round.

Three coordination mechanisms keep the Workers consistent: **topological processing** (Workers partitioned into upstream→downstream levels via Kahn's algorithm), **information exchange** (upstream Workers' fresh parameters get written into the downstream sub-basin's model state before the downstream run), and **parameter locking** (once an upstream Worker has proposed for a round, those values are immutable for the rest of that round).

---

## Calibration strategy

MADR can run an optional **Exploration Stage** followed by one or more **sequential Agent Phases**. The number and content of phases are defined by the model combination — each combination ships its own phase pipeline, chosen so that successive phases isolate a tractable parameter subspace. Every phase ends as soon as either of two conditions fires:

1. **Patience-based early termination** (recommended) — VIC-based combinations track the in-phase mean sub-basin NSE. SWAT+ tracks acceptance of the configured watershed-level joint objective. Multi-candidate rounds are excluded from the counter so aggressive exploration does not trip the trigger.
2. **`max_rounds` cap** — a hard upper bound that always applies, providing a safety ceiling on per-phase cost.

When finite patience is configured, a phase stops as soon as it plateaus rather than always running to its full round budget. Set `patience_rounds` to `999` when a fixed budget is required.

- **Exploration Stage** (optional, before any Agent Rounds): for the VIC-based combinations, a configurable number of simulations per sub-basin is drawn as a mix of Gaussian perturbations around the configured default and Latin Hypercube samples across the feasible range. The shipped Huai River config uses 10 near-center + 40 LHS samples. The per-sub-basin summary (best NSE, top NSE values, best parameter combination) is injected into every Worker's prompt as fixed contextual reference and shown to the Leader once at Round 1. Exploration is **soft contextual evidence**; it does not modify the initial parameter center or search radius. The SWAT+ example leaves this VIC-soil explorer disabled.
- **Multi-candidate generation** (early rounds of selected phases): every Worker is invoked with strategy tags such as `conservative` / `moderate` / `bold`. VIC-based combinations select locally by NSE. SWAT+ evaluates each strategy in one watershed-wide run and adopts one common strategy index using `mean_nse` or `j_total` as configured.
- **Round 1 special case**: the Worker LLM is not invoked. The model runs with the configured default parameters, providing the baseline against which the Leader's first guidance is issued.
- **Final round (force-best)**: after the optimization pipeline ends, one additional verification round restores the per-basin historical best parameters for VIC-based combinations. SWAT+ restores one accepted watershed-level joint bundle, so parameters from different unverified joint runs are never mixed. This final run is outside the configured optimization-round budget.

Both J_total and NSE are retained throughout optimization. J_total combines basin-average NSE with peak- and low-flow consistency, while station-wise and basin-average NSE remain the primary reported performance metrics.

A phase entry in the pipeline looks like this (generic shape — see the Case Study below for a concrete instantiation):

```jsonc
{"name": "PHASE_NAME", "max_rounds": M, "mc_rounds": K, "mc_candidates": 3, "patience_rounds": P}
```

`mc_rounds=K` opens a multi-candidate window over the first K rounds of that phase. The global Round 1 baseline is always single-candidate because no Leader guidance exists yet; therefore `mc_rounds=11` in the first phase means Round 1 uses defaults and Rounds 2–11 are multi-candidate. In later phases, all of the first K rounds use multiple candidates. `patience_rounds` is optional (omit, or set to `999`, to disable early termination and force the full phase budget).

The top-level `max_rounds` is an optional cap on **optimization rounds**, excluding the additional final verification. A value of `0` derives the cap from the sum of the phase-level `max_rounds`; a positive value overrides that sum. The CLI `--max-rounds` option has the highest precedence and uses the same optimization-only semantics.

---

## Pluggable model and LLM components

MADR keeps the hydrological model and the LLM provider behind narrow, swappable interfaces. The static knowledge (parameter sensitivity, domain knowledge, diagnostic rules, basin topology) flows into both the Worker LLM and the Leader LLM each round; the dynamic diagnostic information (round history, search center, phase state) feeds them in parallel. The "Worker LLM" and "Leader LLM" sockets accept any of the supported providers, and the "Model run" block accepts any of the supported model combinations.

![MADR framework — Worker / Leader LLM sockets and Model run as replaceable components](MADR/docs/framework.png)

### Hydrological model combinations

| Combination | Land surface | Routing | Phases | Tunable parameters |
|---|---|---|---|---|
| `vic_routing` (paper default) | [VIC 5.1](https://github.com/UW-Hydro/VIC) | Lohmann (Fortran source in [`routing/`](routing/)) | `VIC_ONLY → ROUTING_ONLY → JOINT` | 6 soil + 2 routing = 8 |
| `vic_cama_flood` | [VIC 5.1](https://github.com/UW-Hydro/VIC) | [CaMa-Flood v4.20](https://github.com/global-hydrodynamics/CaMa-Flood_v4) | `VIC_ONLY → CAMA_FLOOD_ONLY → JOINT` | 6 soil + 4 channel = 10 |
| `swat` | [SWAT+](https://swatplus.gitbook.io/docs) (integrated land + routing) | (built-in) | `SWATPLUS_HRU` | 24 (HRU + soil + aquifer + basin + channel + snow) |
| `wflow` | [wflow_sbm](https://github.com/Deltares/Wflow.jl) (integrated land + kinematic-wave routing) | (built-in) | `WFLOW_SBM` | 8 (soil/routing + snow: KsatHorFrac, KsatVer, f, SoilThickness, N, N_River, TT, Cfmax) |

Adding a new combination is a four-step recipe: drop in `MADR/prompts/<combination>/` (six prompt files), implement `MADR/scripts/executors/<combination>.py` as a `ModelExecutor` subclass, register it in the factory at `MADR/scripts/executors/__init__.py`, and add a sample config under `MADR/configs/`. See `MADR/docs/architecture.md` for the design rationale.

### LLM API adapters

The providers below are tested examples, not an exhaustive compatibility list. Switching among included adapters is config-only: modify `model`, `provider`, and `api_key_env`, then set the corresponding environment variable. Other API-accessible models—including GLM-family services and additional OpenAI- or Anthropic-compatible endpoints—can be connected through configuration or a thin transport adapter. Extending the LLM layer is primarily an API-integration task; adding a hydrological model requires a new executor, parameter semantics, model-specific diagnostic knowledge, and independent physical validation.

| Provider | Example model | API key env var | `timeout_sec` | SDK |
|---|---|---|---|---|
| **Claude** (Anthropic, default) | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` | 240 | `pip install anthropic` |
| **Gemini** (Google) | `gemini-2.5-flash` (or `gemini-3-flash-preview`) | `GEMINI_API_KEY` | 240 | `pip install google-genai` |
| **OpenAI** | `gpt-5.4` / `gpt-5.4-mini` | `OPENAI_API_KEY` | 240 | `pip install openai` |
| **DeepSeek** | `deepseek-v4-pro` / `deepseek-v4-flash` | `DEEPSEEK_API_KEY` | 240 | `pip install openai` |
| **Kimi** (Moonshot) | `kimi-k2.6` | `KIMI_API_KEY` | **300** | `pip install openai httpx` |
| **Qwen** (DashScope) | `qwen3-max` | `DASHSCOPE_API_KEY` | 240 | `pip install openai` |
| **MiniMax** | `MiniMax-M2.5` | `MINIMAX_API_KEY` | 240 | `pip install anthropic` |

Example — switch from Claude to Gemini in any config:

```jsonc
"leader": {
  "model": "gemini-2.5-flash",
  "provider": "gemini",
  "api_key_env": "GEMINI_API_KEY",
  "temperature": 0.3, "max_tokens": 8192, "timeout_sec": 240.0
}
```

For multi-agent (MADR) runs, repeat the same swap in every `workers[]` entry. For SADR (Leader-only ablation), only the `leader` block changes.

**CLI adapters (no API key).** Besides the API adapters above, the same LLM socket accepts local-CLI providers: `"provider": "claude_cli"` drives the installed `claude` command (Claude Code CLI, authenticated through its own login — no `ANTHROPIC_API_KEY` needed), and `"provider": "codex_cli"` drives the `codex` command. Because this is simply "invoke a local CLI binary," the same no-key path works for any provider that ships a CLI — **Kimi** and **Qwen (Qianwen)** included — by pointing the provider at the corresponding command. This is the recommended path when you have a CLI subscription instead of an API key. Because the underlying model is whatever the CLI is logged into, switching LLMs here is only a matter of which CLI you run.

> The example models named in the table above are illustrations captured at one point in time. Providers ship new models frequently, so set `model` to whatever current model you have access to (or select it through your CLI). MADR does not depend on any specific model version.

---

## Reproducibility & run management

Every run writes, into its output directory, the artifacts needed to reproduce and audit it:

- **`provenance/`** — a one-time snapshot at launch: the exact loaded config (content, not just the path) plus `argv`, the full prompt-template tree, the code version (git commit + clean/dirty, or `unknown` outside git), and the Python/platform/host/key-package environment.
- **`llm_calls.jsonl`** — a verbatim, write-before-parse log of every LLM call (system + messages, raw response, finish reason, token usage, duration, caller identity), including truncation retries and failed attempts. Controlled by `MADR_LLM_LOG`, set automatically to `<output_root>/llm_calls.jsonl`; leave it unset to disable.
- **`convergence.png`** — a per-run convergence plot (round vs NSE per station, with rejected multi-candidate points), rendered from Tier-A files so it also works on archived runs.
- **Tiered archiver** — `python -m scripts.archive_run --run-dir <run>` (dry-run by default; add `--apply`) compresses day-scale routing outputs, deletes regenerable working directories and caches, and VACUUMs the checkpoint database, keeping the irreproducible evidence intact and emitting `SHA256SUMS` + `RETENTION.md`.

None of these ever blocks or alters a calibration run; any failure degrades to a warning.

---

## Case study: VIC + Lohmann on the Huai River basin

The paper validates MADR on the catchment upstream of the Bengbu gauging station in the Huai River basin (121,330 km²), with **five nested control stations**: Xixian (XX) → Wangjiaba (WJB) → Bengbu (BB) on the main stem, with Zhoukou (ZK) and Jiangjiaji (JJJ) on tributaries entering between WJB and BB. The example config `MADR/configs/vic_routing_bengbu.json` reproduces this setup.

**Run summary:**

- **Calibration period**: 1981–1990 (1980 spin-up)
- **Validation period**: 1991–1996 (independent transferability test)
- **Paper evaluation budget**: 170 model evaluations per basin
- **LLMs tested**: Claude Sonnet 4.6, Gemini 3 Flash Preview

The shipped configuration runs the calibration period only. The paper's 1991–1996 validation was a separate fixed-parameter evaluation using the calibrated parameter set; it is not launched automatically by the Quick Start command.

**Recommended phase pipeline** for this case (patience enabled):

```jsonc
"phase_config": {
  "pipeline": [
    {"name": "VIC_ONLY",     "max_rounds": 50, "mc_rounds": 11, "mc_candidates": 3, "patience_rounds": 10},
    {"name": "ROUTING_ONLY", "max_rounds": 30, "mc_rounds":  5, "mc_candidates": 3, "patience_rounds":  5},
    {"name": "JOINT",        "max_rounds": 10, "mc_rounds":  0, "mc_candidates": 0, "patience_rounds":  3}
  ]
}
```

The phase pipeline contains 90 optimization rounds. Round 1 is the default baseline; Rounds 2–11 of `VIC_ONLY` and the first five rounds of `ROUTING_ONLY` use three candidates, giving 75 single-candidate optimization rounds and 15 three-candidate rounds. With 50 Exploration simulations and the current build's additional final verification, the maximum is 171 model executions per basin. Patience normally reduces this total. The 170-evaluation results reported below are the fixed-budget experiments archived with the Version 1 preprint; the maintained code makes the final verification explicitly additional rather than silently consuming the last optimization slot.

### Results

![Convergence curves and cost breakdown for MADR (Gemini and Claude) vs. SCE-UA and NSGA-III across the five sub-basins and the basin average, plus wall-clock and token consumption](MADR/docs/convergence.png)

Within its 170-evaluation budget, MADR (Gemini 0.744, Claude 0.760 basin-average NSE) approaches the accuracy that SCE-UA (0.764) and NSGA-III (0.772) achieve only after their full 4,000-evaluation budget — about 23–36× less single-core wall-clock time at ~2.15 M tokens per run. Table 6 shows how many evaluations the conventional algorithms need at the station level to match MADR's final NSE; "—" indicates the threshold was never reached within the 4,000-evaluation budget.

**Table 6.** Number of evaluations the conventional algorithms need to reach MADR's best NSE.

| LLM Source | Algorithm | XX | WJB | JJJ | ZK | BB |
|---|---|---:|---:|---:|---:|---:|
| Gemini | NSGA-III | 1808 | 3004 | 2039 | 252 | 3004 |
| Gemini | SCE-UA | 1745 | 3081 | — | 658 | — |
| Claude | NSGA-III | 1808 | — | 1950 | 3051 | 475 |
| Claude | SCE-UA | 1745 | 3165 | 3037 | 2069 | — |

The full dataset (observations + VIC/Lohmann inputs + 10 × MADR runs × 2 LLMs + SCE-UA and NSGA-III outputs) is in the Zenodo record above.

---

## Quick Start

### Prerequisites

- Python 3.10+
- The compiled binary for whichever combination you intend to use:
  - **VIC + Lohmann**: [VIC 5.1](https://github.com/UW-Hydro/VIC) executable + the Lohmann routing executable from [`routing/`](routing/) (build with `make`)
  - **VIC + CaMa-Flood**: VIC 5.1 + [CaMa-Flood v4.20](https://github.com/global-hydrodynamics/CaMa-Flood_v4) `MAIN_cmf`
  - **SWAT+**: a compiled [SWAT+](https://swatplus.gitbook.io/docs) executable
- Forcing, soil, vegetation, and routing input files (the paper's are in the Zenodo dataset)

### Install Python dependencies

```bash
pip install -r requirements.txt
```

The requirements file includes the LangGraph SQLite checkpointer, NumPy, the tested LLM SDKs, and the pandas/netCDF4 dependencies used by the CaMa-Flood adapter.

### Set your LLM API key

```bash
export ANTHROPIC_API_KEY="your-key"    # default in the shipped configs
# or: GEMINI_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, KIMI_API_KEY,
#     DASHSCOPE_API_KEY, or MINIMAX_API_KEY
```

Or use the **CLI path** — no API key required. Set the config's `provider` to `claude_cli` (or `codex_cli`) and make sure the corresponding CLI (`claude` / `codex`) is installed and logged in.

### Configure model and data paths

The shipped configs use generic `/path/to/...` placeholders. Replace them with paths to your own model installation and dataset before running. For SWAT+, set `MADR_SWATPLUS_CAL_PARMS` if `cal_parms.cal` must be copied into working `TxtInOut` directories that do not already contain it.

### Run

```bash
cd MADR

# VIC + Lohmann (paper default)
python -m scripts.run_langgraph \
    --team-config configs/vic_routing_bengbu.json \
    --output-root ./output/vic_routing_bengbu

# VIC + CaMa-Flood
python -m scripts.run_langgraph \
    --team-config configs/vic_cama_flood_songhua.json \
    --output-root ./output/vic_cama_flood_songhua

# SWAT+
python -m scripts.run_langgraph \
    --team-config configs/swat_yellow.json \
    --output-root ./output/swat_yellow

# WFlow (wflow_sbm)
python -m scripts.run_langgraph \
    --team-config configs/wflow_nested.json \
    --output-root ./output/wflow_nested
```

CLI options: `--team-config PATH`, `--output-root PATH`, `--result-repo PATH`, `--run-name NAME`, `--resume`, `--dry-run`, `--max-rounds N`, `--log-level LEVEL`, `--thread-id ID`.

- `--dry-run` replaces Leader and Worker API calls with deterministic fallbacks; it does **not** skip the hydrological executable. To test orchestration without running VIC, CaMa-Flood, or SWAT+, use a copied config with `model_binding.mode` set to `simulate`.
- `--resume` must use the same `--output-root` and `--thread-id` as the interrupted run because the SQLite checkpoint is stored inside that output directory.
- Use a different output directory for each independent run. `--result-repo PATH --run-name NAME` can create a timestamped run folder automatically; resume that run later by passing its exact folder through `--output-root`.
- `--max-rounds N` caps optimization at N rounds and still reserves one additional force-best verification round.

---

## Configuration

The `MADR/configs/` folder ships four examples:

| File | Combination | Purpose |
|---|---|---|
| `_template.json` | (any) | Annotated template for the common workflow fields, documented inline via `_doc_*` keys |
| `vic_routing_bengbu.json` | `vic_routing` | Paper's Huai River 5-basin setup (Bengbu outlet) |
| `vic_cama_flood_songhua.json` | `vic_cama_flood` | Songhua River nested pair (Jiangqiao → Jiamusi), full real-mode `VIC_ONLY → CAMA_FLOOD_ONLY → JOINT` pipeline |
| `swat_yellow.json` | `swat` | Yellow River Tangnaihai → Lanzhou, real-mode 90-round `SWATPLUS_HRU` pipeline with watershed-level J_total selection |
| `wflow_nested.json` | `wflow` | Nested wflow_sbm example, `WFLOW_SBM` pipeline |

---

## SADR — the ablation control

The sibling `SADR/` folder contains the Single-Agent Diagnostic Reasoning ablation used in the paper. The Leader is identical to MADR's, but the LLM-based Worker is replaced by a deterministic rule-based code executor — the Worker reads the Leader's (direction, magnitude) guidance, maps it through a fixed lookup table into a step around the historical best, and applies a step-decay schedule plus a three-tier escape protocol when a sub-basin stagnates. The LangGraph workflow above shows the SADR-specific nodes in red.

- **Scope**: VIC + Lohmann routing only. SADR is not extended to other model combinations.
- **Purpose**: ablation control that isolates the contribution of the LLM-based Worker. For real applications, use MADR — Worker-level LLM reasoning consistently outperforms the rule-based executor across both Gemini and Claude.
- **Budget**: identical to MADR (170 evaluations per sub-basin) for an apples-to-apples comparison.

---

## Roadmap

The pluggable-executor architecture is built for incremental growth. The `wflow_sbm` combination added in v0.3.0 is the first such extension beyond the original three; future work will verify and integrate **further distributed hydrological models** as new combinations under the same framework, so the same Leader–Worker workflow can drive calibrations of land-surface, channel-routing, and coupled hydrological systems beyond the combinations shipped today.

More broadly, the multi-agent diagnostic-reasoning pattern is well suited to other multi-objective optimization problems in the Earth and water sciences — reservoir operation, water-resource allocation, and broader Earth-system calibration tasks are natural next targets where the same Leader–Worker structure could replace scalar-objective optimization with diagnostic-signal-aware reasoning.

In practice we expect to proceed in stages: first, apply MADR across **more LLMs and more basins** to characterize its generalizability on distributed hydrological models; then extend to adjacent **hydrology and water-resource optimization** problems; and ultimately, as a general diagnostic-reasoning approach to multi-objective optimization, to the **broader Earth sciences**. This release deliberately keeps its scope on distributed hydrological models — the wider directions above are outlook, not yet part of the shipped framework.

---

## Citation

If you use this code, please cite the preprint:

> Yichen Zhao, Ziwei Li, Liujun Zhu, Junliang Jin, Jianyun Zhang. *LLM-Driven Diagnostic Reasoning for Distributed Hydrological Model Calibration*. Research Square, Preprint (Version 1), 22 June 2026. https://doi.org/10.21203/rs.3.rs-10106384/v1

### Companion dataset

> Zhao, Y. (2026). Dataset for "LLM-Driven Diagnostic Reasoning for Distributed Hydrological Model Calibration". Zenodo. https://doi.org/10.5281/zenodo.20622937

---

## License

The MADR framework (this repository, excluding `routing/`) is released under the **MIT License** — see [LICENSE](LICENSE).

The Fortran code in [`routing/`](routing/) is the Lohmann routing model originally distributed by the University of Washington VIC group. It has circulated freely in the research community for many years without a formal license; we include it here with the local modifications documented in [`routing/README.md`](routing/README.md) so the calibration experiments are end-to-end reproducible. Treat that subtree as governed by its upstream provenance, not by the repository-level MIT license.
