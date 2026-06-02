# MADR: Multi-Agent Diagnostic Reasoning for Distributed Hydrological Model Calibration

MADR is an LLM-driven framework for calibrating distributed hydrological models in nested-basin systems. A **Leader Agent** diagnoses cross-station simulation errors and produces structured direction-and-magnitude guidance; per-basin **Worker Agents** translate that guidance into concrete parameter sets. MADR is implemented in [LangGraph](https://github.com/langchain-ai/langgraph) and is evaluated against SCE-UA, NSGA-III, and a single-agent ablation (**SADR**) on the [VIC 5.1.0](https://vic.readthedocs.io/) model coupled with the Lohmann routing model in the Huai River Basin.

This repository contains two architectures:

- **`MADR/`** — Multi-Agent Diagnostic Reasoning (Leader + per-basin Worker LLMs).
- **`SADR/`** — Single-Agent Diagnostic Reasoning ablation (one LLM produces both the global diagnosis and the per-basin parameter values directly, with no separate Worker layer).

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

- **Exploration Stage** (before Agent Rounds): 50 simulations per sub-basin (10 Gaussian perturbations around the configured default + 40 Latin Hypercube samples) over the six VIC soil parameters; routing parameters held at defaults. The per-sub-basin summary (best NSE, top-five NSE, best parameter combination) is injected into every Worker's prompt as a fixed contextual reference and shown to the Leader once at Round 1. The exploration provides **soft contextual evidence** to the LLMs; it does **not** modify the initial parameter center or the search radius.
- **Three-phase Agent Rounds** (90 rounds total):

  | Phase | Rounds | Parameters | Notes |
  |---|---|---|---|
  | **VIC_Only** | 50 | 6 soil params (binfilt, Ds, Dsmax, Ws, soil_d2, soil_d3) | Routing held at defaults |
  | **Routing_Only** | 30 | velocity, diffusivity | VIC held at search center from VIC_Only |
  | **Joint** | 10 | All 8 | Fine-tunes from end of Routing_Only |

- **Multi-candidate generation** (early rounds of VIC_Only and Routing_Only — first 11 and first 5 respectively): every Worker is invoked three times with distinct strategy tags (`conservative`, `moderate`, `bold`); the three candidate parameter sets are simulated and the one achieving the highest NSE is adopted as that round's proposal.
- **Round 1 special case**: the Worker LLM is not invoked. The model runs with the configured default parameters, providing the baseline against which the Leader's first guidance is issued.

Each MADR run performs **170 model evaluations per sub-basin**: 50 in the Exploration Stage + 120 across 90 Agent Rounds (75 single-candidate × 1 + 15 multi-candidate × 3).

---

## Quick Start

### Prerequisites

- Python 3.10+
- [VIC 5.1.0](https://github.com/UW-Hydro/VIC) executable (compiled)
- Lohmann routing model executable
- Forcing, soil, vegetation, and routing input files (archived on Zenodo; see the Citation section)

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

The shipped configs contain the **original author's absolute paths** to executables and data. You must replace them before running. Both configs include a top-level `_path_replacement_notice` field flagging this.

The fields to replace, in both `MADR/configs/bengbu_MADR_default.json` and `SADR/configs/bengbu_SADR_default.json`:

| Field | Description |
|---|---|
| `vic_exe` | Path to your compiled VIC 5.1.0 executable |
| `rout_exe` | Path to your compiled Lohmann routing executable |
| Each `workers[].obs_file` | Observed streamflow file for that station |
| Each `workers[].soil_base` | VIC soil parameter file (`SOIL_PARAM_COMPLETE.txt`) for that sub-basin |
| Each `workers[].global_base` | VIC global parameter file for that sub-basin |
| Each `workers[].rout_template_dir` | Lohmann routing template directory for that sub-basin |
| Each `workers[].rout_exe` | Path to your routing executable |

Tip: most of the data paths share the root `/Users/yc/cowork/framework/`. Search-and-replace this root with your local data root in **both** config files to keep MADR and SADR in sync.

### Run MADR

```bash
cd MADR
python -m scripts.run_langgraph \
    --team-config configs/bengbu_MADR_default.json \
    --output-root ./output
```

### Run SADR (single-agent ablation)

```bash
cd SADR
python -m scripts.run_langgraph \
    --team-config configs/bengbu_SADR_default.json \
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

---

## Project Structure

```
MADR/                                   # Multi-Agent Diagnostic Reasoning
├── configs/bengbu_MADR_default.json    # Run configuration (replace paths before use)
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
├── scripts/                            # Same modules; Worker layer disabled
└── docs/LangGraph_Workflow.png

docs/
└── api_provider_configs.md             # Guide for switching LLM providers
```

---

## SADR (Single-Agent Diagnostic Reasoning)

SADR is the single-agent ablation used in the paper. A single LLM produces both the global diagnosis and the per-sub-basin parameter values directly, without a separate Worker layer. SADR runs under the same computational budget as MADR (170 evaluations per sub-basin) to isolate the contribution of MADR's multi-agent architecture.

SADR's config (`SADR/configs/bengbu_SADR_default.json`) omits `model`, `provider`, and `api_key_env` from worker entries because workers do not invoke an LLM — the Leader handles all reasoning. Apart from this, the project structure, run commands, and CLI options are identical to MADR.

---

## Switching LLM Providers

The default config uses Claude (`claude-sonnet-4-6`). All tested providers (Gemini, Claude, OpenAI, DeepSeek, Grok, Kimi) are supported by `llm_client.py` out of the box — just change the `model`, `provider`, and `api_key_env` fields in the config and set the corresponding environment variable. See [docs/api_provider_configs.md](docs/api_provider_configs.md) for copy-paste-ready snippets and provider-specific notes.

---

## Citation

If you use this code, please cite:

> Zhao, Y., Li, Z., Zhu, L., Jin, J., & Zhang, J. (2026). LLM-Driven Diagnostic Reasoning for Distributed Hydrological Model Calibration. *(manuscript)*

The dataset used in the paper (observed streamflow, VIC and Lohmann input files for 1980–1996) is archived on Zenodo.
