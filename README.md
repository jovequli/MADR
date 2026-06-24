# MADR — Multi-Agent Diagnostic Reasoning for Distributed Hydrological Model Calibration

LLM-driven framework for calibrating distributed hydrological models in nested-basin systems. A Leader agent diagnoses cross-station simulation errors and emits per-basin guidance; per-basin Worker agents translate that guidance into concrete parameter sets. Built on LangGraph, runs through any major LLM provider's native SDK, and treats the underlying hydrological model as a pluggable component.

📦 **Preprint**: Zhao et al. (2026). *LLM-Driven Diagnostic Reasoning for Distributed Hydrological Model Calibration*. Research Square. [10.21203/rs.3.rs-10106384/v1](https://doi.org/10.21203/rs.3.rs-10106384/v1)
📂 **Dataset**: observations, model inputs, all calibration runs, and SCE-UA / NSGA-III benchmarks on Zenodo: [10.5281/zenodo.20622937](https://doi.org/10.5281/zenodo.20622937)

## Repository contents

| Folder | Purpose |
|---|---|
| **[`MADR/`](MADR/)** | The framework. Three model combinations: VIC + Lohmann routing, VIC + CaMa-Flood, SWAT+. See [MADR/README.md](MADR/README.md) for the full guide. |
| **[`SADR/`](SADR/)** | Single-Agent Diagnostic Reasoning ablation — Leader retained, Worker replaced by a deterministic rule-based executor. Used as ablation control only; scope is VIC + Lohmann routing. |
| **[`routing/`](routing/)** | Lohmann routing model Fortran source with this study's local modifications and a Makefile. Build with `cd routing && make`. |

**Start with [`MADR/`](MADR/).** It documents the architecture, the four shipped example configs (`vic_routing_bengbu`, `vic_cama_flood_songhua`, `swat_yellow`, `_template`), how to switch between LLM providers, and how to add a new hydrological-model combination.
