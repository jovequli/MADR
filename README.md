# MADR: Multi-Agent Diagnostic Reasoning for Distributed Hydrological Model Calibration

MADR is an LLM-driven framework for calibrating distributed hydrological models in nested-basin systems. A **Leader Agent** diagnoses cross-station simulation errors and produces structured direction-and-magnitude guidance; per-basin **Worker Agents** translate that guidance into concrete parameter sets. MADR is implemented in [LangGraph](https://github.com/langchain-ai/langgraph) and is evaluated against SCE-UA, NSGA-III, and a single-agent ablation (**SADR**) on the [VIC 5.1.0](https://vic.readthedocs.io/) model coupled with the Lohmann routing model in the Huai River Basin.

This repository contains two architectures:

- **`MADR/`** — Multi-Agent Diagnostic Reasoning. Leader LLM **and** per-basin Worker LLMs.
- **`SADR/`** — Single-Agent Diagnostic Reasoning ablation. The Leader LLM is retained, but the LLM-based Worker is replaced by a deterministic, rule-based code executor, so LLM-driven reasoning occurs only at the Leader level.

📦 **Data**: companion dataset (inputs, all 46 calibration runs, two benchmark methods) is on Zenodo: [10.5281/zenodo.20622937](https://doi.org/10.5281/zenodo.20622937)

---

## MADR Architecture

### Three defining characteristics

1. **Three sources of knowledge** inform diagnostic reasoning: the LLM's pretrained domain knowledge, expert knowledge supplied via prompts and a diagnostic rules table, and iterative understanding built from per-round simulation feedback.
2. **Dynamic information exchanges** among agents, both within a single round and across rounds, continually update the parameter search.
3. **Phased decomposition** divides calibration into successive stages over tractable parameter subspaces, allowing the LLM to focus on a coherent parameter group at lower effective dimensionality.

### Leader / Worker split

Producing global guidance and proposing parameter values for any one sub-basin both draw on pretrained knowledge, expert input, and per-round feedback, but each emphasizes a different aspect. Global guidance emphasizes relationships among sub-basins and how their upstream–downstream interactions determine overall performance. Proposing parameter values for a specific sub-basin emphasizes that sub-basin's own performance history, diagnostic signals, and the latest information transmitted from its upstream neighbors. MADR separates these two kinds of reasoning into two coordinated roles:

- **Leader Agent** — the global-view agent. Activated once at the end of each round to consolidate cross-basin information, perform coordinated diagnostic reasoning over the basins' upstream–downstream topology, and produce per-basin direction-and-magnitude guidance for the next round. The Leader does **not** directly produce parameter values.
- **Worker Agents** — N local-view agents (one per sub-basin). Each Worker is activated at the start of each round to propose the parameter set for that round's simulation, using the Leader's per-basin guidance plus the sub-basin's local history and diagnostic information.

```
                                    ┌─→ Worker xixian    ─→ VIC + routing
                                    ├─→ Worker wangjiaba ─→ VIC + routing
Leader (per-round global diagnosis) ┼─→ Worker jiangjiaji ─→ VIC + routing
                                    ├─→ Worker zhoukou   ─→ VIC + routing
                                    └─→ Worker bengbu    ─→ VIC + routing
```

### One MADR round (four steps)

1. **Worker activation** — The N Worker Agents are invoked in topological order, headwater Workers first. Each Worker uses the Leader's guidance from the previous round to propose parameters for its own sub-basin.
2. **Simulation** — The proposed parameters drive the VIC and Lohmann routing models across the coupled multi-basin system.
3. **Evaluation** — Simulated streamflow is scored using overall objectives, statistical metrics (NSE, KGE, PBIAS, RMSE), and process signatures (peak ratio, peak timing bias, recession K ratio, etc.).
4. **Leader review** — The Leader conducts a global diagnosis of all sub-basin results, identifies cross-basin patterns and bottlenecks, and emits guidance for the next round.

### Worker coordination

Three mechanisms organize coordination among Workers:

- **Topological processing** — Workers are partitioned into topological levels (Kahn's algorithm); all Workers in one level finish before any Worker in the next level begins; Workers within the same level run in parallel.
- **Information exchange** — Once an upstream Worker has proposed its parameters, those values are written into the upstream cells of each downstream sub-basin's hydrological model, so the downstream simulation runs against the most recent upstream state. The downstream Worker sees these upstream effects implicitly, through its own sub-basin's simulation outputs.
- **Parameter locking** — When a downstream Worker proposes its parameters, the values already chosen by its upstream Workers in the same round are held fixed and cannot be revised.

### Calibration strategy

MADR runs an **Exploration Stage** followed by **three sequential agent phases**. Each phase ends as soon as either of two conditions fires:

1. **Patience-based early termination** (the recommended trigger) — the in-phase mean sub-basin NSE has not improved for `patience_rounds` consecutive **single-candidate** rounds. Multi-candidate rounds are excluded from the counter so aggressive exploration does not trip the trigger.
2. **`max_rounds` cap** — a hard upper bound that always applies, providing a safety ceiling on per-phase cost.

This adaptive behavior is the headline change in the current version: each phase now stops as soon as it plateaus, instead of always running to its full round budget. The paper's reported runs can still be reproduced exactly — see the *Reproducing the paper's fixed-budget runs* note below.

- **Exploration Stage** (before Agent Rounds): 50 simulations per sub-basin (10 Gaussian perturbations around the configured default + 40 Latin Hypercube samples) over the six VIC soil parameters; routing parameters held at defaults. The per-sub-basin summary (best NSE, top-five NSE, best parameter combination) is injected into every Worker's prompt as a fixed contextual reference and shown to the Leader once at Round 1. The exploration provides **soft contextual evidence** to the LLMs; it does **not** modify the initial parameter center or the search radius.

- **Three sequential Agent Phases** with both a per-phase cap and a recommended patience trigger:

  | Phase | `max_rounds` (cap) | `patience_rounds` (recommended) | Parameters | Notes |
  |---|---|---|---|---|
  | **VIC-Only** | 50 | 10 | 6 soil params (binfilt, Ds, Dsmax, Ws, soil_d2, soil_d3) | Routing held at defaults |
  | **Routing-Only** | 30 | 5 | velocity, diffusivity | VIC held at search center from VIC-Only |
  | **Joint** | 10 | 3 | All 8 | Fine-tunes from end of Routing-Only |

- **Multi-candidate generation** (early rounds of VIC-Only and Routing-Only — first 11 and first 5 respectively): every Worker is invoked three times with distinct strategy tags (`conservative`, `moderate`, `bold`); the three candidate parameter sets are simulated and the one achieving the highest NSE is adopted as that round's proposal.
- **Round 1 special case**: the Worker LLM is not invoked. The model runs with the configured default parameters, providing the baseline against which the Leader's first guidance is issued.
- **Final round (force-best)**: once the last phase ends — whether by patience or by hitting its `max_rounds` cap — MADR runs one mandatory final round in which each Worker uses its own per-sub-basin best historical parameters. This delivers a clean end-state composite for downstream use.

Recommended pipeline (patience enabled):

```jsonc
"phase_config": {
  "pipeline": [
    {"name": "VIC_ONLY",     "max_rounds": 50, "mc_rounds": 11, "mc_candidates": 3, "patience_rounds": 10},
    {"name": "ROUTING_ONLY", "max_rounds": 30, "mc_rounds":  5, "mc_candidates": 3, "patience_rounds":  5},
    {"name": "JOINT",        "max_rounds": 10, "mc_rounds":  0, "mc_candidates": 0, "patience_rounds":  3}
  ]
}
```

**Evaluation budget per sub-basin.** With patience enabled, each MADR run typically converges in **roughly 110–150 model evaluations per sub-basin**, depending on how quickly each phase plateaus. With patience disabled, the budget is the fixed **170 evaluations** used in the paper.

#### Reproducing the paper's fixed-budget runs

The paper's reported numbers use the legacy fixed-budget configuration: each phase runs to its full `max_rounds` regardless of plateau, giving exactly **170 evaluations per sub-basin** (50 Exploration + 120 across 90 Agent Rounds = 75 single-candidate × 1 + 15 multi-candidate × 3). To reproduce, omit `patience_rounds` from every phase (or set it to `999` to disable the trigger). The shipped `bengbu_MADR_default.json` config reflects this fixed-budget setting so that paper-reported runs are reproducible out of the box.

---

## Quick Start

### Prerequisites

- Python 3.10+
- [VIC 5.1.0](https://github.com/UW-Hydro/VIC) executable (compiled)
- Lohmann routing model executable — Fortran source code is bundled in [`routing/`](routing/) with build instructions and a list of local modifications
- Forcing, soil, vegetation, and routing input files — see the Zenodo dataset (DOI: [10.5281/zenodo.20622937](https://doi.org/10.5281/zenodo.20622937))

### Install Python dependencies

```bash
pip install langgraph anthropic
# Add the SDKs for any other LLM providers you intend to use:
# pip install google-genai       # Gemini
# pip install openai httpx       # OpenAI / DeepSeek / Grok / Kimi
```

### Set your LLM API key

```bash
export ANTHROPIC_API_KEY="your-key"    # default in the shipped configs
# Or one of:
# export GEMINI_API_KEY="your-key"
# export OPENAI_API_KEY="your-key"
# export DEEPSEEK_API_KEY="your-key"
# export GROK_API_KEY="your-key"
# export KIMI_API_KEY="your-key"
```

See [docs/api_provider_configs.md](docs/api_provider_configs.md) for switching providers.

### Replace local paths in the config

The shipped configs contain the **original author's absolute paths** to executables and data. You must replace them before running. Every config includes a top-level `_path_replacement_notice` field flagging this.

The fields to replace, in any config under `MADR/configs/` or `SADR/configs/`:

| Field | Description |
|---|---|
| `vic_exe` | Path to your compiled VIC 5.1.0 executable |
| `rout_exe` | Path to your compiled Lohmann routing executable |
| Each `workers[].obs_file` | Observed streamflow file for that station |
| Each `workers[].soil_base` | VIC soil parameter file (`SOIL_PARAM_COMPLETE.txt`) for that sub-basin |
| Each `workers[].global_base` | VIC global parameter file for that sub-basin |
| Each `workers[].rout_template_dir` | Lohmann routing template directory for that sub-basin |
| Each `workers[].rout_exe` | Path to your routing executable |

Tip: most of the data paths share the root `/Users/yc/cowork/framework/`. Search-and-replace this root with your local data root across all configs to keep them in sync.

### Run MADR

```bash
cd MADR
python -m scripts.run_langgraph \
    --team-config configs/bengbu_MADR_default.json \
    --output-root ./output
```

### Run SADR (Single-Agent Diagnostic Reasoning ablation)

```bash
cd SADR
python -m scripts.run_langgraph \
    --team-config configs/bengbu_SADR_default.json \
    --output-root ./output
```

### Run MADR ablations

```bash
cd MADR
# MADR Without Exploration: skip the 50-evaluation Exploration Stage
python -m scripts.run_langgraph \
    --team-config configs/bengbu_MADR_woexp.json \
    --output-root ./output

# MADR Blank: no phase decomposition, no multi-candidate
python -m scripts.run_langgraph \
    --team-config configs/bengbu_MADR_blank.json \
    --output-root ./output
```

### CLI options

```
--team-config PATH    Path to the config JSON (required)
--output-root PATH    Output directory
--result-repo PATH    Result repo (auto-creates a timestamped subfolder)
--run-name NAME       Run name appended to the output folder
--resume              Resume from the latest checkpoint
--dry-run             Skip LLM calls; use rule-based fallback
--max-rounds N        Override max rounds (0 = use pipeline sum)
--log-level LEVEL     DEBUG, INFO, WARNING, ERROR
--thread-id ID        Thread ID for checkpoint tracking (default: "default")
```

### Per-round outputs

Each completed round writes the following inside its `round_<NNN>/` directory:

| File | Content |
|---|---|
| `round_meta.json` | Round metadata: phase, mean sub-basin NSE, J_total, leader/worker token usage, timings |
| `leader_decision.md` | Leader's J_total, decision, overall assessment, and next-round strategy |
| `results/worker_<basin>.json` | Worker's selected parameters, metrics, score, reasoning text, leader context received; for multi-candidate rounds, each candidate's full parameter set and reasoning is preserved under `mc_detail` |
| `results/worker_<basin>_diagnostic.json` | Structured post-round diagnosis per sub-basin: primary issue, severity, VIC/routing/upstream responsibility split, recommended parameter adjustments, ledger snapshot |
| `results/worker_<basin>_diagnostic.md` | Human-readable version of the same diagnosis |
| `routing_result/<basin>/` | Lohmann routing outputs (per MC candidate when applicable) |

Worker reasoning and post-round diagnoses are produced by the existing loop; no extra LLM calls are made to generate them.

---

## Project Structure

```
MADR/                                   # Multi-Agent Diagnostic Reasoning
├── configs/
│   ├── bengbu_MADR_default.json        # Standard MADR (170 evals / sub-basin)
│   ├── bengbu_MADR_woexp.json          # Ablation: no Exploration Stage (120 evals / sub-basin)
│   └── bengbu_MADR_blank.json          # Ablation: no phase decomposition, no multi-candidate (170 evals)
├── prompts/                            # LLM system prompts
│   ├── leader_system.md
│   ├── leader_review.md
│   ├── param_knowledge_leader.md
│   ├── worker_system.md
│   ├── worker_propose.md
│   └── param_knowledge_worker.md
├── schemas/leader_output.json          # JSON schema for Leader output
├── scripts/                            # Core Python modules
│   ├── run_langgraph.py                # CLI entry point
│   ├── graph_builder.py                # LangGraph graph construction
│   ├── graph_nodes.py                  # Node functions
│   ├── graph_state.py                  # State definition
│   ├── llm_client.py                   # Multi-provider LLM client
│   ├── leader_agent.py                 # Leader agent logic
│   ├── leader_coordinator.py           # Leader coordination
│   ├── worker_agent.py                 # Worker agent logic
│   ├── worker_state.py                 # Worker state
│   ├── worker_ledger.py                # Worker performance tracking
│   ├── worker_diagnostics.py           # NSE, PBIAS, peak metrics, etc.
│   ├── initial_explorer.py             # Exploration Stage
│   ├── phase_config.py                 # Phase management
│   ├── param_validator.py              # Parameter bounds validation
│   ├── routing_utils.py                # Routing model utilities
│   ├── vic_executor.py                 # VIC execution
│   ├── vic_runner.py                   # VIC run management
│   └── context_manager.py              # Context / memory management
└── docs/LangGraph_Workflow.png         # LangGraph workflow diagram

SADR/                                   # Single-Agent Diagnostic Reasoning ablation
├── configs/bengbu_SADR_default.json
├── prompts/                            # Leader-only prompts (no worker prompts)
├── schemas/leader_output.json
├── scripts/                            # Same modules; LLM Worker replaced by a rule-based executor
└── docs/LangGraph_Workflow.png

routing/                                # Lohmann routing model (Fortran source + README)
├── Makefile
├── *.f, *.c, *.h                       # Lohmann routing + UW preprocessing utilities
└── README.md                           # Build instructions and local modifications

docs/
└── api_provider_configs.md             # Guide for switching LLM providers
```

---

## SADR (Single-Agent Diagnostic Reasoning ablation)

SADR retains MADR's Leader-Worker structure but replaces the LLM-based Worker with a **deterministic rule-based executor**, so LLM-driven diagnostic reasoning occurs only at the Leader level. It runs under the same per-sub-basin budget as MADR (170 evaluations) to isolate the contribution of the LLM Worker.

The rule-based Worker:

- Maps the Leader's (direction, magnitude) hint into a step around the historical best, with **small / medium / large = 10% / 22% / 40%** of the parameter's feasible range.
- Applies a **step-decay schedule** that shrinks the effective step size as a phase advances, with phase-specific constants (1.00, 0.05) for VIC-only, (0.40, 0.30) for Routing-only, and (0.15, 0.10) for Joint.
- Includes an **escape protocol**: when a sub-basin's NSE fails to improve beyond the historical best by more than 0.001 for 10 consecutive rounds, the Worker switches to random uniform perturbation under a three-tier escalation schedule (10% → 15% → 20%+ of the feasible range).
- Uses the same multi-candidate mechanism as MADR, with three candidates per round generated by scale factors 0.3 / 0.7 / 1.0 (conservative / moderate / normal).

SADR's config (`SADR/configs/bengbu_SADR_default.json`) omits `model`, `provider`, and `api_key_env` from worker entries because workers do not invoke an LLM. All other run commands and CLI options are identical to MADR.

---

## MADR Ablation Configurations

Two additional MADR ablation variants are provided as separate config files under `MADR/configs/`. Both share the same source code as the standard MADR and differ only in the run configuration.

| Config | What is removed | Per-sub-basin budget |
|---|---|---|
| `bengbu_MADR_woexp.json` | The 50-evaluation Exploration Stage | 120 evaluations (90 Agent Rounds only) |
| `bengbu_MADR_blank.json` | Phase decomposition **and** multi-candidate mechanism | 170 evaluations (50 Exploration + 120 single-candidate Agent Rounds, all 8 parameters tuned simultaneously each round) |

These match the two internal-mechanism ablations reported in the paper.

---

## Switching LLM Providers

The default config uses Claude (`claude-sonnet-4-6`). All tested providers (Gemini, Claude, OpenAI, DeepSeek, Grok, Kimi) are supported by `llm_client.py` out of the box — just change the `model`, `provider`, and `api_key_env` fields in the config and set the corresponding environment variable. See [docs/api_provider_configs.md](docs/api_provider_configs.md) for copy-paste-ready snippets and provider-specific notes.

---

## Roadmap

### Other hydrological models

The current implementation is wired to VIC + Lohmann routing, but the framework itself is model-agnostic. Workers interact with the simulation only through the parameter set they propose and the diagnostic metrics returned from each run. We plan to extend MADR to other distributed hydrological models, including **SWAT**, **wflow**, and **WRF-Hydro**, by swapping the simulation block while keeping the LLM agents, the diagnostic rules table, and the topological scheduling intact.

### Beyond hydrology

More broadly, the multi-agent diagnostic-reasoning pattern is well suited to other multi-objective optimization problems in the Earth and water sciences. We plan to validate it in:

- **Reservoir dispatching** — balancing flood control, water supply, hydropower, and ecological flow objectives across cascaded reservoirs.
- **Water resource allocation** — coordinating allocations across competing demands, regions, and basins under multi-objective constraints.
- **Other Earth-science calibration problems** — land-surface, groundwater, and coupled Earth-system models, where process-level diagnostic signals can guide LLM-driven parameter tuning under multi-objective trade-offs.

---

## Citation

If you use this code, please cite:

> Zhao, Y., Li, Z., Zhu, L., Jin, J., & Zhang, J. (2026). LLM-Driven Diagnostic Reasoning for Distributed Hydrological Model Calibration. *(manuscript under review)*

### Dataset

The companion dataset (observation streamflow, VIC and Lohmann input files for 1980–1996, plus all calibration outputs for MADR / SADR / ablations / SCE-UA / NSGA-III) is available on Zenodo:

> Zhao, Y. (2026). Dataset for "LLM-Driven Diagnostic Reasoning for Distributed Hydrological Model Calibration". Zenodo. https://doi.org/10.5281/zenodo.20622937
