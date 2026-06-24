# Multi-model architecture for MADR.cli

**Date:** 2026-06-23
**Scope:** Make MADR.cli support multiple model combinations side-by-side, starting with VIC+Routing (existing) and VIC+CaMa-Flood (new).

## Goals

- Existing VIC+Routing runs continue to work bit-identical with no config changes.
- A new `vic_cama_flood` combination runs end-to-end on the Huaihe basin (`bengbu_1990-1996_025deg`).
- Prompt files, parameter knowledge, and the model-execution layer are all swappable per combination.
- Patience-based phase termination and the mandatory force-best final round are preserved.
- All new code is portable Python — no macOS-only shell idioms, paths stay relative or come from config.
- SWAT integration (from `MADR_CC/`) can be folded in later as a third combination using the same structure.

## Out of scope

- Switching CLI back to HTTP API (stays on `claude_cli` provider for now).
- SWAT integration this session.
- Touching the published `MADR-main/` and `MADR/` GitHub copies; this work lives only in `MADR.cli/`.

## Section 1 — Directory layout & prompt selector

```
MADR.cli/
├── prompts/
│   ├── vic_routing/                  ← existing 6 files moved here
│   └── vic_cama_flood/               ← new; same 6-file shape
├── configs/                          ← existing configs unchanged
│                                       (new "model_combination" field, default "vic_routing")
└── scripts/executors/                ← new module dir (Section 2)
```

- `leader_agent.py` / `worker_agent.py` resolve `PROMPTS_DIR = scripts/../prompts/<combination>/` from a new `model_combination` config field. Default `"vic_routing"` preserves backward compat.
- Phase pipeline names stay JSON-configurable; `get_tunable_keys(phase)` becomes per-combination via the executor's `tunable_keys_by_phase` property.
- VIC+CaMa-Flood pipeline: `VIC_ONLY → CAMA_FLOOD_ONLY → JOINT`.

## Section 2 — Executor abstraction

New module `scripts/executors/`:

```
ModelExecutor (ABC)                    in scripts/executors/__init__.py
├── VICRoutingExecutor                 in scripts/executors/vic_routing.py
└── VICCamaFloodExecutor               in scripts/executors/vic_cama_flood.py

Factory: make_executor(combination, configs, paths) → ModelExecutor
```

ABC surface:

- `run(subbasin_id, run_id, candidate_id, params, phase_tag, **kwargs) → (ok, metrics)`
- `cache_intermediates_for_routing(centers, output_root, state, worker_cfgs)` — called at LAND→ROUTING transition
- `clear_intermediate_cache()` — called at ROUTING→JOINT transition
- `tunable_keys_by_phase` (property) → `{phase_name: set[param_name]}`

`VICRoutingExecutor` is a thin shim that delegates to today's `vic_executor.py` + Lohmann routing (no behaviour change).

`graph_nodes.py` switches from `vic_executor.*` to `executor.*`. `_SHARED["vic_executor"]` becomes `_SHARED["executor"]` (with a back-compat alias kept for one cycle).

## Section 3 — VIC+CaMa-Flood execution pipeline

Per-candidate evaluation:

1. Worker proposes `{6 VIC soil params, Hc, Hmin, Wc, Wmin}`.
2. **VIC run** — uses VIC soil params, produces grid fluxes (`huaihe_fluxes_*.txt`).
3. **NetCDF conversion** — wraps `cama-flood-run` skill's `vic_post/process_data_windows_ymd.py` to write `runoff_YYYY.nc`.
4. **Channel-param write** — channel param shell script (`src_param/s01-channel_params.sh`) rewritten with the candidate's `Hc, Hmin, Wc, Wmin`. Other channel params keep defaults from the skill.
5. **CaMa-Flood run** — execute `MAIN_cmf` against the prepared Huaihe basin map.
6. **Metrics** — read CaMa output (`outflw.bin` or NetCDF), compute NSE / KGE / PBIAS / peak_ratio / lowflow_ratio at the bengbu outlet station; same metric schema as VIC+Routing.

Cache strategy mirrors VIC+Routing:

- `VIC_ONLY`: full pipeline each round
- VIC→CAMA transition: `cache_intermediates_for_routing` saves the winning VIC fluxes per basin
- `CAMA_FLOOD_ONLY`: skip VIC, re-use cached fluxes, re-run NetCDF conversion + CaMa
- CAMA→JOINT transition: `clear_intermediate_cache` forces full pipeline
- `JOINT`: full pipeline with all 10 params tunable

Parameter ranges for the 4 CaMa channel params (from user, no published canonical):

| Param | Range | Meaning |
|---|---|---|
| Hc | 0.01 – 1 | channel depth coefficient |
| Hmin | 0.5 – 2 | minimum channel depth |
| Wc | 0.1 – 4 | channel width coefficient |
| Wmin | 1 – 10 | minimum channel width |

## Section 4 — Map preparation (Huaihe)

The `cama-flood-run` skill's 3-step workflow runs once before any calibration:

1. **Region cut** (`src_region/cut_domain` + `combine_hires`) — extract Huaihe bounding box from `glb_15min/`. Output: `map/huaihe_15min/`.
2. **Input matrix** (`src_param/generate_inpmat`) — VIC grid → CaMa grid mapping.
3. **Channel param baseline** (`calc_outclm` + `calc_rivwth`) — compute `Qave` climatology and bankfull geometry per channel cell.

These produce `map/huaihe_15min/` once. Calibration only overwrites the 4 channel coefficients in `src_param/s01-channel_params.sh` each round.

## Section 5 — Testing & rollout

1. After Phase 1 (prompts only): import-level smoke test.
2. After Phase 2 (executor abstraction): run a tiny VIC+Routing sim (`--dry-run` or 1 real round) to confirm no regression.
3. After Phase 3a (map ready) + 3b (CaMa executor implemented): single-candidate dry-run on Huaihe with default channel params — confirm CaMa-Flood binary completes and outflw.bin is produced.
4. After Phase 3c (prompts wired): 2-3 round calibration run on Huaihe with `model_combination: "vic_cama_flood"`.

## Risks

- CaMa-Flood binary may need basin-specific tuning of unrelated params (manning, fldhgt) — expectation is that defaults work for a first integration test.
- VIC output time-step / unit conventions must match what `vic_post/process_data_windows_ymd.py` expects. Verify against the skill notes.
- CaMa-Flood at 15min resolution gives coarser hydrographs than Lohmann at 0.25° — NSE values may be lower; that's expected and is the whole point of comparing.

## Linux portability

- All new shell calls use POSIX `sh` semantics (no `bash` arrays, no `sed -i ''`).
- Paths constructed via `pathlib.Path`, never `os.system("rm ...")` or `cp -r`.
- No reliance on macOS-only tools (`pkill`, `lsof`, `mdfind`).
- Subprocess calls pass an explicit `cwd=` so they work regardless of where the CLI was launched.
