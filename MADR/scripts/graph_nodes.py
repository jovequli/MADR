#!/usr/bin/env python3
"""LangGraph node functions for MADR calibration.

Each function takes CalibrationState and returns a partial state update dict.
All domain logic is delegated to existing modules; these nodes are pure
orchestration glue.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .graph_state import CalibrationState, PhaseSpec
from .phase_config import (
    load_phase_pipeline,
    check_phase_transition,
    should_use_multi_candidate,
    get_total_max_rounds,
)
from .context_manager import ContextManager, compress_history, compress_diagnostics

from .llm_client import LLMClient, LLMClientConfig, LLMResponse, create_client
from .param_validator import (
    PARAM_KEYS,
    DEFAULT_RANGES,
    CalibrationPhase,
    get_tunable_keys,
    get_locked_keys,
    enforce_phase_locks,
    normalize_params,
    validate_params,
    safe_apply_leader_guidance,
    fallback_rule_based_candidates,
    generate_candidates_from_proposals,
    apply_adjustments,
    clamp,
    validate_leader_output,
)
from .routing_utils import UpstreamRoutingInfo, find_direc_in_template
from .vic_runner import UpstreamSoilInfo
from .leader_agent import LeaderAgent, LeaderMemory
from .worker_agent import WorkerAgent, WorkerMemory
from .vic_executor import VICExecutor, VICConfig
from .leader_coordinator import LeaderCoordinator
from .worker_diagnostics import diagnose_round

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility (same as orchestrator)
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _to_native(obj):
    """Recursively convert numpy types to native Python for msgpack serialization."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_to_native(v) for v in obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, obj: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def _append_jsonl(path: Path, obj: Dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def _score_row(metrics: Dict[str, float]) -> float:
    return float(metrics.get("NSE", -1))


# ---------------------------------------------------------------------------
# Shared objects cache (created once per graph invocation)
# ---------------------------------------------------------------------------

_SHARED: Dict[str, Any] = {}


def _get_shared(state: CalibrationState) -> Dict[str, Any]:
    """Lazily build shared objects (leader, workers, vic_executor, etc.)."""
    if _SHARED.get("_initialized"):
        return _SHARED

    cfg = state["team_config"]
    worker_cfgs = state["worker_cfgs"]
    output_root = Path(state["output_root"])
    dry_run = state.get("dry_run", False)
    mode = state.get("mode", "simulate")
    param_ranges = state["param_ranges"]
    range_lists = {k: list(v) if isinstance(v, tuple) else v
                   for k, v in param_ranges.items()}

    # Leader
    leader_cfg = cfg["leader"]
    if dry_run:
        leader_llm = _DummyLLMClient("dry-run-leader")
    else:
        leader_llm = _create_llm_client(leader_cfg)
    leader = LeaderAgent(
        agent_id=leader_cfg["agent_id"],
        llm_client=leader_llm,
        workers=worker_cfgs,
        objective_weights=state.get("objective_weights", {}),
        param_ranges=range_lists,
    )

    # Workers
    workers: Dict[str, WorkerAgent] = {}
    for w_cfg in worker_cfgs:
        sid = w_cfg["subbasin_id"]
        if dry_run:
            w_llm = _DummyLLMClient(f"dry-run-worker-{sid}")
        else:
            w_llm = _create_llm_client(w_cfg)
        workers[sid] = WorkerAgent(
            agent_id=w_cfg["agent_id"],
            subbasin_id=sid,
            station_code=w_cfg.get("station_code", sid),
            llm_client=w_llm,
            is_outlet=w_cfg.get("is_outlet", False),
            neighbors_downstream=w_cfg.get("neighbors_downstream", []),
            param_ranges=range_lists,
            window_size=int(w_cfg.get("memory_window", 5)),
        )
        workers[sid].set_output_dir(str(output_root))

    # VIC executor
    vic_executor = VICExecutor(mode=mode)
    if mode == "real":
        for w_cfg in worker_cfgs:
            sid = w_cfg["subbasin_id"]
            vic_cfg = VICConfig(
                basin_name=sid,
                base_output_dir=str(output_root),
                soil_base=w_cfg.get("soil_base", ""),
                global_base=w_cfg.get("global_base", ""),
                obs_file=w_cfg.get("obs_file", ""),
                rout_template_dir=w_cfg.get("rout_template_dir", ""),
                vic_exe=w_cfg.get("vic_exe", cfg.get("vic_exe", "")),
                rout_exe=w_cfg.get("rout_exe", cfg.get("rout_exe", "")),
                start_year=cfg.get("simulation_period", {}).get("start_year", 1979),
                end_year=cfg.get("simulation_period", {}).get("end_year", 1980),
                flux_prefix=w_cfg.get("flux_prefix", "huaihe_fluxes_"),
                vic_flux_source=w_cfg.get("vic_flux_source", ""),
            )
            vic_executor.register_basin(sid, vic_cfg)

        # Validate all VIC paths before any execution
        issues = vic_executor.validate_all()
        if issues:
            for basin, missing in issues.items():
                logger.error("VIC config [%s] missing paths: %s", basin, missing)
            raise RuntimeError(
                f"VIC path validation failed for {len(issues)} basin(s): "
                + ", ".join(f"{b}({len(m)} missing)" for b, m in issues.items())
            )

    # Leader coordinator
    upstream_map = state["upstream_map"]
    coordinator = LeaderCoordinator(
        upstream_map=upstream_map,
        outlet_id=cfg.get("outlet_subbasin_id", "bengbu"),
    )

    _SHARED.update({
        "_initialized": True,
        "leader": leader,
        "workers": workers,
        "vic_executor": vic_executor,
        "coordinator": coordinator,
    })
    return _SHARED


def _create_llm_client(agent_cfg: Dict) -> LLMClient:
    return create_client(
        model=agent_cfg["model"],
        provider=agent_cfg.get("provider", ""),
        temperature=agent_cfg.get("temperature", 0.3),
        max_tokens=agent_cfg.get("max_tokens", 4096),
        json_mode=True,
        api_key_env=agent_cfg.get("api_key_env", ""),
        timeout_sec=agent_cfg.get("timeout_sec", 120.0),
        max_retries=agent_cfg.get("max_retries", 3),
    )


class _DummyLLMClient:
    def __init__(self, model="dry-run"):
        self.config = type("C", (), {
            "model": model, "temperature": 0, "max_tokens": 0, "json_mode": True
        })()

    def chat(self, messages=None, system="", **kwargs):
        return LLMResponse(content="{}", provider="dry-run", model="dry-run")


# ---------------------------------------------------------------------------
# Topology helpers
# ---------------------------------------------------------------------------

def _build_upstream_map(workers: List[Dict]) -> Dict[str, List[str]]:
    upstream: Dict[str, List[str]] = {w["subbasin_id"]: [] for w in workers}
    for w in workers:
        for ds in w.get("neighbors_downstream", []):
            if ds in upstream:
                upstream[ds].append(w["subbasin_id"])
    return upstream


def _compute_topo_levels(upstream_map: Dict[str, List[str]]) -> List[List[str]]:
    in_degree = {sid: len(ups) for sid, ups in upstream_map.items()}
    downstream_of: Dict[str, List[str]] = {sid: [] for sid in upstream_map}
    for sid, ups in upstream_map.items():
        for up in ups:
            if up in downstream_of:
                downstream_of[up].append(sid)
    levels: List[List[str]] = []
    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    while queue:
        levels.append(sorted(queue))
        next_queue = []
        for sid in queue:
            for ds in downstream_of.get(sid, []):
                in_degree[ds] -= 1
                if in_degree[ds] == 0:
                    next_queue.append(ds)
        queue = next_queue
    return levels


def _collect_all_upstream(subbasin_id: str, upstream_map: Dict,
                          full_upstream_map: Dict) -> List[str]:
    umap = full_upstream_map if full_upstream_map else upstream_map
    visited: set = set()
    stack = list(umap.get(subbasin_id, []))
    while stack:
        up = stack.pop()
        if up not in visited:
            visited.add(up)
            stack.extend(umap.get(up, []))
    return list(visited)


def _build_upstream_routing_info(
    subbasin_id: str, state: CalibrationState, worker_cfgs: List[Dict],
) -> List[UpstreamRoutingInfo]:
    upstream_map = state["upstream_map"]
    full_upstream_map = state.get("full_upstream_map", {})
    fixed_upstream_routing = state.get("fixed_upstream_routing", {})
    topo_levels = state["topo_levels"]
    centers = state["centers"]

    all_upstream = _collect_all_upstream(subbasin_id, upstream_map, full_upstream_map)
    if not all_upstream:
        return []

    level_of: Dict[str, int] = {}
    for level_idx, level_sids in enumerate(topo_levels):
        for s in level_sids:
            level_of[s] = level_idx
    if full_upstream_map:
        umap = full_upstream_map
        in_deg = {sid: len(ups) for sid, ups in umap.items()}
        ds_of: Dict[str, List[str]] = {sid: [] for sid in umap}
        for sid, ups in umap.items():
            for up in ups:
                if up in ds_of:
                    ds_of[up].append(sid)
        queue = deque([sid for sid, d in in_deg.items() if d == 0])
        lvl = 0
        while queue:
            size = len(queue)
            for _ in range(size):
                sid = queue.popleft()
                if sid not in level_of:
                    level_of[sid] = lvl
                for ds in ds_of.get(sid, []):
                    in_deg[ds] -= 1
                    if in_deg[ds] == 0:
                        queue.append(ds)
            lvl += 1

    all_upstream.sort(key=lambda s: level_of.get(s, 0), reverse=True)

    worker_cfg_by_sid = {w["subbasin_id"]: w for w in worker_cfgs}
    infos: List[UpstreamRoutingInfo] = []
    for up_sid in all_upstream:
        up_cfg = worker_cfg_by_sid.get(up_sid)
        if up_cfg:
            rout_dir = up_cfg.get("rout_template_dir", "")
            direc_path = find_direc_in_template(rout_dir) if rout_dir else ""
            up_center = centers.get(up_sid, {})
            infos.append(UpstreamRoutingInfo(
                direc_path=direc_path,
                velocity=up_center.get("velocity", 1.5),
                diffusivity=up_center.get("diffusivity", 800.0),
            ))
        elif up_sid in fixed_upstream_routing:
            fixed = fixed_upstream_routing[up_sid]
            rout_dir = fixed.get("rout_template_dir", "")
            direc_path = find_direc_in_template(rout_dir) if rout_dir else ""
            infos.append(UpstreamRoutingInfo(
                direc_path=direc_path,
                velocity=float(fixed.get("velocity", 1.5)),
                diffusivity=float(fixed.get("diffusivity", 800.0)),
            ))
    return infos


def _build_upstream_soil_info(
    subbasin_id: str, state: CalibrationState, worker_cfgs: List[Dict],
) -> List[UpstreamSoilInfo]:
    upstream_map = state["upstream_map"]
    full_upstream_map = state.get("full_upstream_map", {})
    topo_levels = state["topo_levels"]
    centers = state["centers"]

    all_upstream = _collect_all_upstream(subbasin_id, upstream_map, full_upstream_map)
    if not all_upstream:
        return []

    level_of: Dict[str, int] = {}
    for level_idx, level_sids in enumerate(topo_levels):
        for s in level_sids:
            level_of[s] = level_idx
    if full_upstream_map:
        umap = full_upstream_map
        in_deg = {sid: len(ups) for sid, ups in umap.items()}
        ds_of: Dict[str, List[str]] = {sid: [] for sid in umap}
        for sid, ups in umap.items():
            for up in ups:
                if up in ds_of:
                    ds_of[up].append(sid)
        queue = deque([sid for sid, d in in_deg.items() if d == 0])
        lvl = 0
        while queue:
            size = len(queue)
            for _ in range(size):
                sid = queue.popleft()
                if sid not in level_of:
                    level_of[sid] = lvl
                for ds in ds_of.get(sid, []):
                    in_deg[ds] -= 1
                    if in_deg[ds] == 0:
                        queue.append(ds)
            lvl += 1

    all_upstream.sort(key=lambda s: level_of.get(s, 0), reverse=True)

    worker_cfg_by_sid = {w["subbasin_id"]: w for w in worker_cfgs}
    infos: List[UpstreamSoilInfo] = []
    for up_sid in all_upstream:
        up_cfg = worker_cfg_by_sid.get(up_sid)
        if up_cfg:
            soil_base = up_cfg.get("soil_base", "")
            up_center = centers.get(up_sid, {})
            soil_params = {k: up_center[k] for k in
                           ("binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3")
                           if k in up_center}
            if soil_base and soil_params:
                infos.append(UpstreamSoilInfo(
                    soil_base=soil_base,
                    params=soil_params,
                ))
    return infos


# ---------------------------------------------------------------------------
# Anti-flip direction momentum
# ---------------------------------------------------------------------------

def _apply_anti_flip(
    direction_history: Dict[str, Dict],
    sid: str,
    hints: List[Dict],
    round_improved: bool,
) -> Tuple[List[Dict], Dict[str, Dict]]:
    """Enforce direction momentum. Returns (filtered_hints, updated_direction_history)."""
    MIN_FAIL_TO_FLIP = 3
    hist = dict(direction_history.get(sid, {}))
    filtered = []

    for h in hints:
        p = h.get("parameter", h.get("param", ""))
        new_dir = h.get("direction", "hold")
        new_mag = h.get("magnitude", "none")

        if new_dir == "hold" or new_mag == "none":
            filtered.append(h)
            continue

        prev = hist.get(p)
        if prev is None:
            hist[p] = {"dir": new_dir, "streak": 1, "fail_count": 0}
            filtered.append(h)
            continue

        prev_dir = prev["dir"]
        same_direction = (new_dir == prev_dir)

        if same_direction:
            prev["streak"] += 1
            if round_improved:
                prev["fail_count"] = 0
            else:
                prev["fail_count"] += 1
            filtered.append(h)
        else:
            if round_improved:
                blocked = dict(h)
                blocked["direction"] = prev_dir
                blocked["_antiflip"] = f"blocked: prev {prev_dir} improved"
                prev["streak"] += 1
                prev["fail_count"] = 0
                filtered.append(blocked)
            elif prev["fail_count"] < MIN_FAIL_TO_FLIP:
                blocked = dict(h)
                blocked["direction"] = "hold"
                blocked["magnitude"] = "none"
                blocked["_antiflip"] = f"hold: only {prev['fail_count']} fails"
                prev["fail_count"] += 1
                filtered.append(blocked)
            else:
                hist[p] = {"dir": new_dir, "streak": 1, "fail_count": 0}
                filtered.append(h)

    updated = dict(direction_history)
    updated[sid] = hist
    return filtered, updated


# ===========================================================================
# NODE FUNCTIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. init_calibration
# ---------------------------------------------------------------------------

def init_calibration(state: CalibrationState) -> Dict[str, Any]:
    """Initialize calibration: load config, create agents, set up state."""
    cfg = state["team_config"]
    worker_cfgs = cfg["workers"]
    output_root = Path(state["output_root"])
    _ensure_dir(output_root)

    # Topology
    upstream_map = _build_upstream_map(worker_cfgs)
    topo_levels = _compute_topo_levels(upstream_map)

    # Parameter ranges
    param_ranges = {}
    for k, v in cfg.get("param_ranges", DEFAULT_RANGES).items():
        if isinstance(v, (list, tuple)):
            param_ranges[k] = (float(v[0]), float(v[1]))
        else:
            param_ranges[k] = DEFAULT_RANGES.get(k, (0, 1))

    # Initial centers/radii
    initial_center = normalize_params(cfg.get("initial_param_center", {}), param_ranges)
    initial_radius = dict(cfg.get("initial_radius", {}))
    centers = {w["subbasin_id"]: dict(initial_center) for w in worker_cfgs}
    radii = {w["subbasin_id"]: dict(initial_radius) for w in worker_cfgs}

    # Phase pipeline
    phase_pipeline = load_phase_pipeline(cfg)
    max_rounds = state.get("max_rounds", 0) or get_total_max_rounds(phase_pipeline)

    # Objective weights
    objective_weights = cfg.get("objective_weights", {"mean_nse": 0.90, "consistency": 0.10})

    # MC config
    mc_cfg = cfg.get("multi_candidate", {})

    # Phase NSE thresholds
    phase_nse_thresholds = cfg.get("phase_config", {}).get("phase_nse_thresholds", {})

    # Direction history init
    direction_history = {w["subbasin_id"]: {} for w in worker_cfgs}

    # Worker memories init
    worker_memories = {
        w["subbasin_id"]: WorkerMemory(
            w["subbasin_id"], w.get("station_code", w["subbasin_id"])
        ).to_checkpoint()
        for w in worker_cfgs
    }

    # Save run manifest
    _write_json(output_root / "run_manifest.json", {
        "team_config": state.get("team_config_path", ""),
        "output_root": str(output_root),
        "mode": state.get("mode", "simulate"),
        "max_rounds": max_rounds,
        "objective_weights": objective_weights,
        "initial_phase": phase_pipeline[0]["name"] if phase_pipeline else "VIC_ONLY",
        "phase_pipeline": phase_pipeline,
        "initial_centers": centers,
        "initial_radii": radii,
        "topo_levels": topo_levels,
        "start_time_utc": _utc_now(),
    })

    # Initialize shared objects
    _SHARED.clear()

    print(f"  Calibration initialized: {len(worker_cfgs)} basins, "
          f"{len(phase_pipeline)} phases, {max_rounds} total rounds")

    return {
        "worker_cfgs": worker_cfgs,
        "upstream_map": upstream_map,
        "full_upstream_map": cfg.get("full_upstream_map", {}),
        "fixed_upstream_routing": {
            e["subbasin_id"]: e for e in cfg.get("fixed_upstream_routing", [])
        },
        "topo_levels": topo_levels,
        "param_ranges": param_ranges,
        "objective_weights": objective_weights,
        "phase_pipeline": phase_pipeline,
        "phase_nse_thresholds": phase_nse_thresholds,
        "max_rounds": max_rounds,
        "max_parallel_workers": int(cfg.get("max_parallel_workers", 4)),
        "centers": centers,
        "radii": radii,
        "direction_history": direction_history,
        "current_phase_idx": 0,
        "current_phase_name": phase_pipeline[0]["name"] if phase_pipeline else "VIC_ONLY",
        "rounds_in_current_phase": 0,
        "phase_history": [],
        "round_id": 0,
        "leader_memory_rounds": [],
        "leader_j_history": [],
        "leader_outlet_nse_history": [],
        "leader_mean_sub_nse_history": [],
        "leader_best_global": {"score": float("-inf"), "round_id": -1, "params_by_subbasin": {}},
        "leader_latest_best_by_subbasin": {},
        "leader_token_budget": int(cfg.get("leader_token_budget", 50000)),
        "worker_memories": worker_memories,
        "comm_totals": {"leader_to_worker": 0, "worker_to_leader": 0, "worker_to_worker": 0},
        "mc_round_summaries": {},
        "mc_vic_rounds": int(mc_cfg.get("vic_rounds", 0)),
        "mc_rout_rounds": int(mc_cfg.get("rout_rounds", 0)),
        "mc_n_candidates": int(mc_cfg.get("n_candidates", 5)),
        "escape_states": {},
        "exploration_results": {},
        "exploration_summary": "",
        "should_continue": True,
        "force_final_round": False,
        "best_msn_in_phase": float("-inf"),
        "rounds_since_improvement_in_phase": 0,
    }


# ---------------------------------------------------------------------------
# 2. check_exploration / run_exploration
# ---------------------------------------------------------------------------

def check_exploration(state: CalibrationState) -> Dict[str, Any]:
    """Check if initial exploration is needed (pass-through node)."""
    return {}


def run_exploration(state: CalibrationState) -> Dict[str, Any]:
    """Run initial exploration (pure code, zero LLM calls).

    If `initial_exploration.seed_from_run` is set in config, reuses a
    previous run's per-basin exploration JSONs and only rebuilds the
    summary string — no new VIC simulations are launched.
    """
    from .initial_explorer import (
        run_initial_exploration,
        format_exploration_summary,
        _compact_params,
    )

    cfg = state["team_config"]
    explore_cfg = cfg.get("initial_exploration", {})
    output_root = Path(state["output_root"])

    # ── Path A: reuse a previous run's exploration ──
    seed_run = explore_cfg.get("seed_from_run", "")
    if seed_run:
        seed_dir = Path(seed_run)
        seed_exp_dir = seed_dir / "exploration"
        if not seed_exp_dir.exists():
            raise RuntimeError(
                f"seed_from_run path missing exploration/ folder: {seed_run}"
            )

        SOIL_KEYS = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3"]
        results: Dict[str, Dict[str, Any]] = {}
        for sid_file in sorted(seed_exp_dir.glob("*.json")):
            sid = sid_file.stem
            if sid.startswith("_"):
                continue  # skip our own metadata files
            data = json.load(open(sid_file))
            samples = data.get("samples", []) or []
            if samples:
                sorted_samples = sorted(samples, key=lambda s: s.get("nse", -999), reverse=True)
                best = sorted_samples[0]
            else:
                best = {
                    "nse": data.get("best_nse", 0),
                    "params": data.get("best_params", {}),
                    "label": "seed",
                }
            top5 = sorted(samples, key=lambda s: s.get("nse", -999), reverse=True)[:5]
            top5_nse = [s.get("nse", -999) for s in top5]
            top5_str = ", ".join(f"{n:.3f}" for n in top5_nse)
            summary_text = (
                f"{sid}: best_NSE={best.get('nse', data.get('best_nse', 0)):.4f} "
                f"({best.get('label', 'seed')}) | top5=[{top5_str}] | "
                f"params={_compact_params(best.get('params', data.get('best_params', {})), SOIL_KEYS)}"
            )
            results[sid] = {
                "best_params": data.get("best_params", best.get("params", {})),
                "best_nse": data.get("best_nse", best.get("nse", 0)),
                "best_metrics": best.get("metrics", {}),
                "all_results": samples,
                "n_samples": data.get("n_samples", len(samples)),
                "summary_text": summary_text,
            }

        summary = format_exploration_summary(results)

        # Persist a copy in the new run so downstream tooling still sees expected files
        explore_out = {
            sid: {"best_nse": r["best_nse"], "best_params": r["best_params"],
                  "n_samples": r["n_samples"], "summary": r["summary_text"]}
            for sid, r in results.items()
        }
        _write_json(output_root / "initial_exploration.json", explore_out)
        new_explore_dir = output_root / "exploration"
        _ensure_dir(new_explore_dir)
        _write_json(new_explore_dir / "_seed_info.json", {
            "seed_from_run": str(seed_dir),
            "basins": list(results.keys()),
        })
        for sid, r in results.items():
            _write_json(new_explore_dir / f"{sid}.json", {
                "subbasin_id": sid,
                "n_samples": r["n_samples"],
                "best_nse": r["best_nse"],
                "best_params": r["best_params"],
                "samples": r["all_results"],
                "_seeded_from": str(seed_dir),
            })

        print(f"  >>> Exploration reused from seed: {seed_dir.name}")
        for sid, r in results.items():
            print(f"      [{sid}] best_NSE={r['best_nse']:.4f}, {r['n_samples']} seed samples")
        return _to_native({
            "exploration_results": explore_out,
            "exploration_summary": summary,
            "centers": state["centers"],
        })

    # ── Path B: run new 50-sample exploration (existing logic) ──
    shared = _get_shared(state)

    # The explorer needs an orchestrator-like interface
    # We create a lightweight adapter
    class _ExplorerAdapter:
        def __init__(self):
            self.vic_executor = shared["vic_executor"]
            self.workers = shared["workers"]
            self.leader = shared["leader"]
            self.worker_cfgs = state["worker_cfgs"]
            self.centers = dict(state["centers"])
            self.radii = dict(state["radii"])
            self.param_ranges = state["param_ranges"]
            self.upstream_map = state["upstream_map"]
            self.topo_levels = state["topo_levels"]
            self.current_phase = CalibrationPhase(state["current_phase_name"])
            self.output_root = output_root
            self.full_upstream_map = state.get("full_upstream_map", {})
            self.fixed_upstream_routing = state.get("fixed_upstream_routing", {})

        def _build_upstream_routing_info(self, sid):
            return _build_upstream_routing_info(sid, state, self.worker_cfgs)

        def _build_upstream_soil_info(self, sid):
            return _build_upstream_soil_info(sid, state, self.worker_cfgs)

    adapter = _ExplorerAdapter()

    results = run_initial_exploration(
        orchestrator=adapter,
        n_near=int(explore_cfg.get("n_near", 10)),
        n_random=int(explore_cfg.get("n_random", 20)),
    )
    summary = format_exploration_summary(results)

    # Save summary
    explore_out = {
        sid: {"best_nse": r["best_nse"], "best_params": r["best_params"],
              "n_samples": r["n_samples"], "summary": r["summary_text"]}
        for sid, r in results.items()
    }
    _write_json(output_root / "initial_exploration.json", explore_out)

    # Save full per-basin exploration results (all 31 samples)
    explore_dir = output_root / "exploration"
    _ensure_dir(explore_dir)
    for sid, r in results.items():
        _write_json(explore_dir / f"{sid}.json", {
            "subbasin_id": sid,
            "n_samples": r["n_samples"],
            "best_nse": r["best_nse"],
            "best_params": r["best_params"],
            "samples": r.get("all_results", []),
        })

    return _to_native({
        "exploration_results": explore_out,
        "exploration_summary": summary,
        "centers": state["centers"],
    })



# ---------------------------------------------------------------------------
# 3. round_start
# ---------------------------------------------------------------------------

def round_start(state: CalibrationState) -> Dict[str, Any]:
    """Begin a new calibration round.

    A round is the FINAL force-best round if either:
      - `force_final_round` flag was set by check_phase_transition_node
        (last phase ended via patience or max_rounds), OR
      - the global max_rounds safety cap is reached.
    """
    round_id = state.get("round_id", 0) + 1
    max_rounds = state["max_rounds"]
    force_final = state.get("force_final_round", False)
    is_final = force_final or (round_id >= max_rounds)
    phase_name = state["current_phase_name"]

    print(f"\n{'='*60}")
    print(f"  ROUND {round_id}/{max_rounds}  |  Phase: {phase_name}")
    if is_final:
        print(f"  ★ FINAL ROUND — using historical best params")
    print(f"{'='*60}\n")

    # Round directory
    output_root = Path(state["output_root"])
    round_dir = output_root / f"round_{round_id:03d}"
    for subdir in ("tasks", "results", "messages"):
        _ensure_dir(round_dir / subdir)

    return {
        "round_id": round_id,
        "is_final_round": is_final,
        "round_start_time": _utc_now(),
        "round_dir": str(round_dir),
        "rounds_in_current_phase": state.get("rounds_in_current_phase", 0) + 1,
        "current_level_idx": 0,
        "round_worker_results": [],
        "batch_worker_results": [],
        "mc_round_summaries": dict(state.get("mc_round_summaries", {})),
        "force_final_round": False,  # consume the trigger flag
    }


# ---------------------------------------------------------------------------
# 4. prepare_batch
# ---------------------------------------------------------------------------

def prepare_batch(state: CalibrationState) -> Dict[str, Any]:
    """Prepare the current topo-level batch for execution."""
    level_idx = state["current_level_idx"]
    topo_levels = state["topo_levels"]

    if level_idx >= len(topo_levels):
        # No more levels — signal done
        return {"current_batch_sids": []}

    sids = topo_levels[level_idx]
    print(f"  Batch {level_idx + 1}/{len(topo_levels)}: {sids}")

    # Check multi-candidate
    pipeline = state["phase_pipeline"]
    phase_idx = state["current_phase_idx"]
    rounds_in_phase = state["rounds_in_current_phase"]
    use_multi, n_cands = should_use_multi_candidate(pipeline, phase_idx, rounds_in_phase)

    if state.get("is_final_round", False):
        use_multi = False

    # R1 (no leader decision yet) → skip MC, use exact defaults
    if use_multi and state.get("leader_decision") is None:
        use_multi = False

    if use_multi:
        print(f"  [v2] Multi-candidate mode: {n_cands} candidates/basin")

    return {
        "current_batch_sids": sids,
        "use_multi_candidate": use_multi,
        "mc_n_candidates": n_cands,
        "batch_worker_results": [],  # reset for this batch
    }


# ---------------------------------------------------------------------------
# 5. execute_batch_workers (parallel via ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def execute_batch_workers(state: CalibrationState) -> Dict[str, Any]:
    """Execute all workers in the current batch in parallel.

    Uses ThreadPoolExecutor for parallelism (same as original orchestrator).
    This avoids Send() state-copy overhead.
    """
    sids = state.get("current_batch_sids", [])
    if not sids:
        return {"batch_worker_results": []}

    round_id = state["round_id"]
    shared = _get_shared(state)
    vic_executor: VICExecutor = shared["vic_executor"]
    worker_cfgs = state["worker_cfgs"]
    phase = CalibrationPhase(state["current_phase_name"])
    force_best = state.get("is_final_round", False)
    use_multi = state.get("use_multi_candidate", False) and not force_best
    max_parallel = state.get("max_parallel_workers", 4)
    round_dir = Path(state["round_dir"])

    batch_results = []
    mc_lines = []

    def _run_one_worker(sid):
        worker: WorkerAgent = shared["workers"][sid]
        center = state["centers"][sid]
        radius = state["radii"][sid]

        # Restore worker memory
        wmem_data = state.get("worker_memories", {}).get(sid)
        if wmem_data:
            worker.memory = WorkerMemory.from_checkpoint(wmem_data)

        # Inject per-basin exploration summary for Worker prompt (matches original MADR)
        full_explore = state.get("exploration_summary", "")
        if full_explore:
            basin_lines = [ln for ln in full_explore.split("\n") if sid in ln]
            worker._exploration_summary = "\n".join(basin_lines) if basin_lines else ""
        else:
            worker._exploration_summary = ""

        # Leader guidance
        prev_decision = state.get("leader_decision")
        guidance = None
        if prev_decision:
            guidance = shared["leader"].get_worker_guidance(prev_decision, sid)

        # Anti-flip
        raw_hints = guidance.get("param_adjustments", []) if guidance else []
        direction_history = state.get("direction_history", {})
        prev_improved = False
        if round_id > 1:
            ledger_path = Path(state["output_root"]) / f"{sid}_ledger.jsonl"
            if ledger_path.exists():
                last_line = ""
                with open(ledger_path) as _lf:
                    for last_line in _lf:
                        pass
                if last_line.strip():
                    try:
                        prev_improved = json.loads(last_line).get("improved", False)
                    except json.JSONDecodeError:
                        pass
        filtered_hints, _ = _apply_anti_flip(direction_history, sid, raw_hints, prev_improved)

        if guidance:
            guidance = dict(guidance)
            guidance["param_adjustments"] = filtered_hints

        # Upstream info
        up_routing_info = _build_upstream_routing_info(sid, state, worker_cfgs)
        up_soil_info = _build_upstream_soil_info(sid, state, worker_cfgs)

        # R1 (no leader decision yet) → exact defaults, skip MC even if enabled
        if use_multi and prev_decision is not None:
            result, mc_line = _run_worker_multi(
                worker, vic_executor, round_id, center, radius,
                guidance, round_dir, state.get("mc_n_candidates", 5),
                phase, up_routing_info, up_soil_info,
            )
            return result, mc_line
        else:
            result = _run_worker_single(
                worker, vic_executor, round_id, center, radius,
                guidance, round_dir, phase, up_routing_info, up_soil_info,
                force_best=force_best,
            )
            return result, ""

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {pool.submit(_run_one_worker, sid): sid for sid in sids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                result, mc_line = future.result()
                batch_results.append(result)
                if mc_line:
                    mc_lines.append(mc_line)
                nse = result.get("metrics", {}).get("NSE", float("nan"))
                print(f"    Worker {sid}: NSE={nse:.4f}, score={result.get('score_local', 0):.4f}"
                      f"{'  [MC]' if use_multi else ''}")
            except Exception as e:
                logger.error("Worker %s failed: %s\n%s", sid, e, traceback.format_exc())
                batch_results.append({
                    "subbasin_id": sid,
                    "station_code": "",
                    "agent_id": f"worker_{sid}",
                    "best_params": state["centers"][sid],
                    "params": state["centers"][sid],
                    "metrics": {"NSE": float("nan"), "RMSE": float("nan"),
                               "PBIAS": float("nan")},
                    "score_local": float("-inf"),
                    "analysis": f"Worker failed: {e}",
                    "llm_tokens": {"prompt": 0, "completion": 0, "total": 0},
                    "duration_sec": 0,
                })

    # Store MC summaries
    mc_summaries = dict(state.get("mc_round_summaries", {}))
    if mc_lines:
        prev = mc_summaries.get(round_id, "")
        mc_summaries[round_id] = (prev + "\n" + "\n".join(mc_lines)).strip()

    return _to_native({
        "batch_worker_results": batch_results,
        "mc_round_summaries": mc_summaries,
    })


def _run_worker_single(
    worker, vic_executor, round_id, center, radius, guidance,
    round_dir, phase, up_routing, up_soil, force_best=False,
) -> Dict:
    """Run a single-candidate worker round. Mirrors run_worker_round()."""
    sid = worker.subbasin_id
    t0 = dt.datetime.now(dt.timezone.utc)
    locked_keys = set(get_locked_keys(phase))
    phase_str = phase.value
    worker_tokens = {"prompt": 0, "completion": 0, "total": 0}
    reasoning = ""              # populated only when the LLM is called
    leader_context = ""         # populated only when guidance is present

    if force_best:
        best_record = worker.ledger.get_best_params()
        if best_record and best_record.get("params"):
            candidate_list = [dict(best_record["params"])]
            reasoning = "force_best: reused historical best params per basin"
        else:
            candidate_list = [dict(center)]
            reasoning = "force_best: no ledger history, used current center"
    elif guidance and guidance.get("param_adjustments"):
        if guidance:
            priority = guidance.get("priority_focus", "")
            adj_hints = guidance.get("param_adjustments", [])
            hint_strs = [
                f"{a.get('param', '?')}: {a.get('direction', '?')} ({a.get('magnitude', '')})"
                for a in adj_hints[:4]
            ]
            leader_context = f"{priority}. Hints: {', '.join(hint_strs)}" if hint_strs else priority

        # Pass historical best metrics (matching center params) instead of last round metrics
        best_record = worker.ledger.get_best_params()
        best_metrics = best_record.get("metrics", {}) if best_record else {}

        candidate_list, reasoning, worker_tokens = worker.propose_params(
            round_id=round_id,
            current_center=center,
            metrics=best_metrics,
            phase=phase_str,
            leader_context=leader_context,
        )
    else:
        # Round 1 (no guidance): use exact default params with zero noise
        candidate_list = [dict(center)]
        reasoning = "R1 baseline: default params (no LLM call this round)"

    for cand in candidate_list:
        for k in locked_keys:
            cand[k] = center.get(k, cand.get(k, 0))
        cand.update(normalize_params(cand, worker.ranges))

    params = candidate_list[0]
    ok, metrics = vic_executor.run(
        subbasin_id=sid,
        run_id=round_id * 100 + 1,
        candidate_id=1,
        params=params,
        phase_tag=f"round_{round_id}_c1",
        upstream_routing_info=up_routing,
        upstream_soil_info=up_soil,
    )
    score = ok and _score_row(metrics) or float("-inf")
    duration = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()

    # Save routing result
    vic_cfg = vic_executor._vic_configs.get(sid)
    if ok and vic_cfg:
        src = Path(vic_cfg.base_output_dir) / f"{sid}_calibration_ai" / "current_run" / "routing_result"
        if src.exists():
            dst = round_dir / "routing_result" / sid
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(str(src), str(dst))

    _write_json(round_dir / "results" / f"worker_{sid}.json", {
        "round_id": round_id, "subbasin_id": sid,
        "result": {"params": params, "metrics": metrics, "ok": ok, "score_local": round(score, 6)},
        "reasoning": reasoning,
        "leader_context_received": leader_context,
        "llm_tokens": worker_tokens,
        "duration_sec": round(duration, 3),
    })

    return {
        "subbasin_id": sid,
        "station_code": worker.station_code,
        "agent_id": worker.agent_id,
        "best_params": params,
        "params": params,
        "metrics": metrics,
        "score_local": round(score, 6),
        "analysis": "1-candidate run.",
        "llm_tokens": worker_tokens,
        "duration_sec": round(duration, 3),
    }


def _run_worker_multi(
    worker, vic_executor, round_id, center, radius, guidance,
    round_dir, n_candidates, phase, up_routing, up_soil,
) -> Tuple[Dict, str]:
    """Run multi-candidate worker round (MADR: LLM-based).

    1. Call Worker LLM n_candidates times (conservative / moderate / bold).
    2. Each call produces a full parameter set directly.
    3. Run all candidates through VIC and pick the best.
    4. Record which strategy won — fed back in next round's history.
    """
    MC_STRATEGIES = ["conservative", "moderate", "bold", "exploratory", "reverse"]

    sid = worker.subbasin_id
    t0 = dt.datetime.now(dt.timezone.utc)
    locked_keys = set(get_locked_keys(phase))
    phase_str = phase.value
    total_tokens = {"prompt": 0, "completion": 0, "total": 0}

    # ── Step 1: Build leader context ──
    leader_context = ""
    if guidance:
        priority = guidance.get("priority_focus", "")
        adj_hints = guidance.get("param_adjustments", [])
        hint_strs = [
            f"{a.get('param', '?')}: {a.get('direction', '?')} ({a.get('magnitude', '')})"
            for a in adj_hints[:4]
        ]
        leader_context = f"{priority}. Hints: {', '.join(hint_strs)}" if hint_strs else priority

    # Historical best metrics for LLM context
    best_record = worker.ledger.get_best_params()
    best_metrics = best_record.get("metrics", {}) if best_record else {}

    # ── Step 2: Call Worker LLM n_candidates times with different strategies ──
    strategies = MC_STRATEGIES[:n_candidates]
    candidates = []

    for strategy in strategies:
        strategy_context = f"{leader_context}\n[Strategy: {strategy} — "
        if strategy == "conservative":
            strategy_context += "make small, safe adjustments to 1-2 params only]"
        elif strategy == "moderate":
            strategy_context += "make moderate adjustments to 2-3 key params]"
        elif strategy == "bold":
            strategy_context += "make large, aggressive adjustments to address main issues]"
        elif strategy == "exploratory":
            strategy_context += "try a different direction than previous rounds]"
        else:
            strategy_context += "reverse a previous change that may have hurt performance]"

        try:
            cand_list, reasoning, wtokens = worker.propose_params(
                round_id=round_id,
                current_center=center,
                metrics=best_metrics,
                phase=phase_str,
                leader_context=strategy_context,
            )
            for k in total_tokens:
                total_tokens[k] += wtokens.get(k, 0)
            if cand_list:
                cand = cand_list[0]
                candidates.append((cand, strategy, reasoning))
                logger.info("Worker %s MC-%s proposal: %s", sid, strategy,
                            {k: round(v, 4) for k, v in cand.items()}.__repr__()[:120])
            else:
                logger.warning("Worker %s MC-%s returned empty, skipping.", sid, strategy)
        except Exception as e:
            logger.warning("Worker %s MC-%s failed (%s), skipping.", sid, strategy, e)

    # Fallback: if all LLM calls failed, use center as single candidate
    if not candidates:
        logger.warning("Worker %s all MC proposals failed, using center as fallback.", sid)
        candidates = [(dict(center), "fallback", "all MC LLM calls failed; using current center")]

    # ── Step 3: Evaluate all candidates ──
    eval_results = []
    vic_cfg = vic_executor._vic_configs.get(sid)
    for i, (params, label, reasoning) in enumerate(candidates):
        ok, metrics = vic_executor.run(
            subbasin_id=sid,
            run_id=round_id * 100 + i + 1,
            candidate_id=i + 1,
            params=params,
            phase_tag=f"round_{round_id}_mc{i+1}_{label}",
            upstream_routing_info=up_routing,
            upstream_soil_info=up_soil,
        )
        # Save this candidate's routing result before next run overwrites it
        if ok and vic_cfg:
            src = Path(vic_cfg.base_output_dir) / f"{sid}_calibration_ai" / "current_run" / "routing_result"
            if src.exists():
                dst = round_dir / "routing_result" / sid / f"c{i+1}_{label}"
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(str(src), str(dst))
        nse = metrics.get("NSE", float("-inf")) if ok else float("-inf")
        score = ok and _score_row(metrics) or float("-inf")
        eval_results.append({
            "idx": i, "params": params, "metrics": metrics,
            "ok": ok, "score": score, "nse": nse, "label": label,
            "reasoning": reasoning,
        })
        print(f"    [{sid}] MC candidate {i+1}/{len(candidates)} ({label}): NSE={nse:.4f}")

    eval_results.sort(key=lambda x: x["score"], reverse=True)
    best = eval_results[0]

    step_parts = [f"{r['label']}→{r['nse']:.3f}" for r in eval_results]
    summary_line = f"{sid}: best={best['label']}, " + ", ".join(step_parts)

    duration = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()

    # Build mc_detail with full metrics so post_round_diagnostics can derive
    # a per-candidate DiagnosisResult for every alternative, not just the winner.
    mc_detail = [
        {
            "idx": r["idx"] + 1,
            "label": r["label"],
            "nse": r["nse"],
            "score": r["score"],
            "ok": r.get("ok", True),
            "params": r["params"],
            "metrics": r.get("metrics", {}),
            "reasoning": r.get("reasoning", ""),
        }
        for r in eval_results
    ]

    result = {
        "subbasin_id": sid,
        "station_code": worker.station_code,
        "agent_id": worker.agent_id,
        "best_params": best["params"],
        "params": best["params"],
        "metrics": best["metrics"],
        "score_local": round(best["score"], 6),
        "analysis": f"MC({len(candidates)},LLM): best={best['label']}",
        "llm_tokens": total_tokens,
        "duration_sec": round(duration, 3),
        "mc_detail": mc_detail,
        "leader_context_received": leader_context,
    }

    _ensure_dir(round_dir / "results")
    _write_json(round_dir / "results" / f"worker_{sid}.json", {
        "round_id": round_id, "subbasin_id": sid,
        "result": {"params": best["params"], "metrics": best["metrics"],
                   "ok": best["ok"], "score_local": round(best["score"], 6)},
        "winning_strategy": best["label"],
        "winning_reasoning": best.get("reasoning", ""),
        "leader_context_received": leader_context,
        "mc_detail": mc_detail,
        "llm_tokens": total_tokens,
        "duration_sec": round(duration, 3),
    })

    return result, summary_line


# ---------------------------------------------------------------------------
# 6. collect_batch_results
# ---------------------------------------------------------------------------

def collect_batch_results(state: CalibrationState) -> Dict[str, Any]:
    """Collect batch results and accumulate into round results."""
    batch_results = list(state.get("batch_worker_results", []))
    round_results = list(state.get("round_worker_results", []))
    round_results.extend(batch_results)

    # Inter-batch center update
    centers = dict(state["centers"])
    locked_keys = set(get_locked_keys(CalibrationPhase(state["current_phase_name"])))
    param_ranges = state["param_ranges"]

    # Update latest_best and centers
    latest_best = dict(state.get("leader_latest_best_by_subbasin", {}))
    shared = _get_shared(state)

    for result in batch_results:
        bsid = result["subbasin_id"]
        bnse = result.get("metrics", {}).get("NSE", float("-inf"))
        worker = shared["workers"].get(bsid)

        # Restore worker memory for ledger
        wmem_data = state.get("worker_memories", {}).get(bsid)
        if wmem_data and worker:
            worker.memory = WorkerMemory.from_checkpoint(wmem_data)

        worker_best = worker.ledger.get_best_nse() if worker else float("-inf")
        if bnse >= worker_best:
            new_c = normalize_params(result.get("best_params", centers[bsid]), param_ranges)
            for k in locked_keys:
                new_c[k] = centers[bsid].get(k, new_c.get(k, 0))
            centers[bsid] = new_c

        # Update latest_best
        prev_best = latest_best.get(bsid, {})
        prev_nse = prev_best.get("metrics", {}).get("NSE", float("-inf"))
        if bnse >= prev_nse or not prev_best:
            latest_best[bsid] = {
                "round_id": state["round_id"],
                "params": result.get("best_params", result.get("params", {})),
                "metrics": result.get("metrics", {}),
            }

    return _to_native({
        "round_worker_results": round_results,
        "centers": centers,
        "leader_latest_best_by_subbasin": latest_best,
        "batch_worker_results": [],  # clear for next batch
    })


# ---------------------------------------------------------------------------
# 7. inter_batch_center_update (implicit in collect_batch_results)
# ---------------------------------------------------------------------------

def check_more_batches(state: CalibrationState) -> str:
    """Conditional edge: check if there are more topo levels to process.

    Note: advance_batch already incremented current_level_idx,
    so we check if the current index is still within bounds.
    """
    current_idx = state["current_level_idx"]
    if current_idx < len(state["topo_levels"]):
        return "prepare_batch"
    return "post_round_diagnostics"


def advance_batch(state: CalibrationState) -> Dict[str, Any]:
    """Move to the next topo level."""
    return {"current_level_idx": state["current_level_idx"] + 1}


# ---------------------------------------------------------------------------
# 8. post_round_diagnostics
# ---------------------------------------------------------------------------

def post_round_diagnostics(state: CalibrationState) -> Dict[str, Any]:
    """Run worker diagnostics and cross-basin coordination."""
    shared = _get_shared(state)
    workers = shared["workers"]
    coordinator: LeaderCoordinator = shared["coordinator"]
    worker_results = state["round_worker_results"]
    result_by_sid = {r["subbasin_id"]: r for r in worker_results}
    upstream_map = state["upstream_map"]
    round_id = state["round_id"]

    diagnostic_reports = []
    for result in worker_results:
        sid = result["subbasin_id"]
        worker = workers.get(sid)
        if not worker:
            continue

        # Restore memory
        wmem_data = state.get("worker_memories", {}).get(sid)
        if wmem_data:
            worker.memory = WorkerMemory.from_checkpoint(wmem_data)

        prev_metrics = None
        last_entry = worker.ledger.get_last_entry() if hasattr(worker.ledger, 'get_last_entry') else None
        if last_entry:
            prev_metrics = last_entry.get("metrics")

        is_headwater = len(upstream_map.get(sid, [])) == 0
        upstream_quality = "good"
        for up_sid in upstream_map.get(sid, []):
            up_result = result_by_sid.get(up_sid, {})
            up_nse = up_result.get("metrics", {}).get("NSE", 0)
            if isinstance(up_nse, (int, float)) and up_nse < 0.5:
                upstream_quality = "poor"
                break

        report = worker.report_diagnostics(
            metrics=result.get("metrics", {}),
            prev_metrics=prev_metrics,
            context={"is_headwater": is_headwater, "upstream_quality": upstream_quality},
        )
        diagnostic_reports.append(report)

    # Cross-basin insights
    coordinator_summaries = []
    for report in diagnostic_reports:
        diag = report.get("diagnosis", {})
        ledger = report.get("ledger_summary", {})
        coordinator_summaries.append({
            "subbasin_id": report.get("subbasin_id"),
            "current_nse": report.get("current_metrics", {}).get("NSE"),
            "primary_issue": diag.get("primary_issue", "none"),
            "improvement_trend": diag.get("improvement_trend", "?"),
            "stagnation_risk": ledger.get("stagnation_count", 0) >= 3,
            "stagnation_count": ledger.get("stagnation_count", 0),
            "top_recommendations": [
                f"{r['param']} {r['direction']}" for r in diag.get("recommendations", [])[:3]
            ],
            "suggested_direction": diag.get("reasoning", ""),
            "routing_responsibility": diag.get("routing_responsibility", 0),
        })

    coordination_result = coordinator.compute_cross_basin_insights(coordinator_summaries)

    # NSE history by basin
    nse_history_by_basin = coordinator.build_nse_history_by_basin(
        state.get("leader_memory_rounds", [])
    )

    diagnostics_prompt_block = coordinator.format_diagnostics_for_leader(
        diagnostic_reports=diagnostic_reports,
        coordination=coordination_result,
        j_history=state.get("leader_j_history", []),
        nse_history_by_basin=nse_history_by_basin,
    )

    # Inject exploration summary for round 1
    diag_block = diagnostics_prompt_block or ""
    if round_id <= 1 and state.get("exploration_summary", ""):
        diag_block = state["exploration_summary"] + "\n\n" + diag_block

    # Inject MC summary
    mc_summary = state.get("mc_round_summaries", {}).get(round_id, "")
    if mc_summary:
        diag_block += "\n\n### Multi-Candidate Step-Size Response\n" + mc_summary

    # Persist per-worker diagnostics so they survive the run.
    # These reports are otherwise consumed only by the next leader prompt
    # and discarded.
    round_dir = Path(state["round_dir"])
    _ensure_dir(round_dir / "results")
    from .worker_diagnostics import diagnose_round
    for report in diagnostic_reports:
        sid = report.get("subbasin_id", "unknown")
        _write_json(
            round_dir / "results" / f"worker_{sid}_diagnostic.json",
            report,
        )
        _write_worker_diagnostic_md(round_dir, round_id, report)

        # For multi-candidate rounds, additionally diagnose every candidate
        # (not just the winner) and write per-candidate files matching the
        # routing_result/<basin>/c{i}_<label>/ naming convention. No new LLM
        # calls — diagnose_round() is pure code, same path the winner used.
        wr = result_by_sid.get(sid, {})
        mc_detail = wr.get("mc_detail", []) or []
        if len(mc_detail) > 1:
            worker = workers.get(sid)
            if worker is None:
                continue
            prev_metrics = None
            last_entry = worker.ledger.get_last_entry() if hasattr(worker.ledger, "get_last_entry") else None
            if last_entry:
                prev_metrics = last_entry.get("metrics")
            is_headwater = len(upstream_map.get(sid, [])) == 0
            up_q = "good"
            for up_sid in upstream_map.get(sid, []):
                up_nse = result_by_sid.get(up_sid, {}).get("metrics", {}).get("NSE", 0)
                if isinstance(up_nse, (int, float)) and up_nse < 0.5:
                    up_q = "poor"
                    break
            ledger_summary = worker.ledger.summary()
            for cand in mc_detail:
                cand_metrics = cand.get("metrics") or {}
                if not cand_metrics:
                    continue
                cand_diag = diagnose_round(
                    cand_metrics, prev_metrics,
                    {"is_headwater": is_headwater, "upstream_quality": up_q},
                )
                idx = cand.get("idx", 0)
                label = cand.get("label", "cand")
                slug = f"c{idx}_{label}"
                cand_report = {
                    "subbasin_id": sid,
                    "station_code": worker.station_code,
                    "candidate_idx": idx,
                    "candidate_label": label,
                    "candidate_reasoning": cand.get("reasoning", ""),
                    "is_winner": idx == mc_detail[0]["idx"] if mc_detail else False,
                    "diagnosis": {
                        "primary_issue": cand_diag.primary_issue,
                        "severity": cand_diag.severity,
                        "severity_score": cand_diag.severity_score,
                        "vic_responsibility": cand_diag.vic_responsibility,
                        "routing_responsibility": cand_diag.routing_responsibility,
                        "upstream_responsibility": cand_diag.upstream_responsibility,
                        "reasoning": cand_diag.reasoning,
                        "improvement_trend": cand_diag.improvement_trend,
                        "recommendations": cand_diag.recommendations,
                    },
                    "current_metrics": cand_metrics,
                    "ledger_summary": ledger_summary,
                }
                _write_json(
                    round_dir / "results" / f"worker_{sid}_{slug}_diagnostic.json",
                    cand_report,
                )
                _write_worker_diagnostic_md(round_dir, round_id, cand_report, slug=slug)

    return {
        "diagnostic_reports": diagnostic_reports,
        "diagnostics_prompt_block": diag_block,
        "coordination_result": coordination_result.to_dict() if hasattr(coordination_result, 'to_dict') else {},
    }


def _write_worker_diagnostic_md(
    round_dir: Path,
    round_id: int,
    report: Dict[str, Any],
    slug: Optional[str] = None,
) -> None:
    """Render the per-round per-worker diagnostic as a human-readable Markdown.

    Mirrors the leader_decision.md style. All content comes from data the
    worker already produced this round — no extra LLM calls.

    When `slug` is provided (e.g. "c2_moderate"), this is a per-candidate
    file from a multi-candidate round; the filename and heading include the
    candidate tag so the file fits alongside the winner's diagnostic.
    """
    sid = report.get("subbasin_id", "unknown")
    diag = report.get("diagnosis", {}) or {}
    metrics = report.get("current_metrics", {}) or {}
    ledger = report.get("ledger_summary", {}) or {}
    cand_label = report.get("candidate_label")
    cand_reasoning = report.get("candidate_reasoning", "")
    is_winner = report.get("is_winner", False)

    def _f(v, fmt=".4f"):
        if v is None:
            return "N/A"
        try:
            return format(float(v), fmt)
        except (TypeError, ValueError):
            return str(v)

    if cand_label:
        heading = f"# Round {round_id} Worker {sid} Diagnostic [{slug or cand_label}{' — winner' if is_winner else ''}]\n"
    else:
        heading = f"# Round {round_id} Worker {sid} Diagnostic\n"

    lines = [
        heading,
        "## Current Metrics",
        f"- NSE: {_f(metrics.get('NSE'))}",
        f"- PBIAS: {_f(metrics.get('PBIAS'), '.2f')}",
        f"- peak_ratio: {_f(metrics.get('peak_ratio'), '.3f')}",
        f"- lowflow_ratio: {_f(metrics.get('lowflow_ratio'), '.3f')}",
        f"- peak_timing_bias_days: {_f(metrics.get('peak_timing_bias_days'), '.1f')}",
        "",
        "## Diagnosis",
        f"- Primary issue: {diag.get('primary_issue', 'N/A')} ({diag.get('severity', 'N/A')})",
        f"- Severity score: {_f(diag.get('severity_score'), '.2f')}",
        f"- Improvement trend: {diag.get('improvement_trend', 'N/A')}",
        f"- VIC responsibility: {_f(diag.get('vic_responsibility'), '.0%')}",
        f"- Routing responsibility: {_f(diag.get('routing_responsibility'), '.0%')}",
        f"- Upstream responsibility: {_f(diag.get('upstream_responsibility'), '.0%')}",
        "",
        f"### Reasoning",
        f"{diag.get('reasoning', 'N/A')}",
    ]

    if cand_reasoning:
        lines += ["", "### Worker proposal reasoning", cand_reasoning]

    recs = diag.get("recommendations", []) or []
    if recs:
        lines += ["", "## Recommendations"]
        for i, r in enumerate(recs, 1):
            param = r.get("param", "?")
            direction = r.get("direction", "?")
            magnitude = r.get("magnitude", "")
            note = r.get("note", "") or r.get("reason", "")
            mag_str = f" ({magnitude})" if magnitude else ""
            note_str = f" — {note}" if note else ""
            lines.append(f"{i}. **{param}** {direction}{mag_str}{note_str}")

    if ledger:
        lines += [
            "",
            "## Ledger summary",
            f"- Best NSE so far: {_f(ledger.get('best_nse'))}",
            f"- Stagnation count: {ledger.get('stagnation_count', 0)}",
            f"- Total entries: {ledger.get('total_entries', 0)}",
        ]

    fname = f"worker_{sid}_{slug}_diagnostic.md" if slug else f"worker_{sid}_diagnostic.md"
    with (round_dir / "results" / fname).open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 9. leader_review (stateless — no conversation accumulation)
# ---------------------------------------------------------------------------

def leader_review(state: CalibrationState) -> Dict[str, Any]:
    """Have Leader LLM review the round. Stateless — no conversation history."""
    shared = _get_shared(state)
    leader: LeaderAgent = shared["leader"]
    round_id = state["round_id"]
    worker_results = state["round_worker_results"]
    phase = CalibrationPhase(state["current_phase_name"])

    # Restore leader memory from state
    leader.memory = LeaderMemory()
    leader.memory.rounds = list(state.get("leader_memory_rounds", []))
    leader.memory.j_history = list(state.get("leader_j_history", []))
    leader.memory.outlet_nse_history = list(state.get("leader_outlet_nse_history", []))
    leader.memory.mean_sub_nse_history = list(state.get("leader_mean_sub_nse_history", []))
    leader.memory.best_global = dict(state.get("leader_best_global", {
        "score": float("-inf"), "round_id": -1, "params_by_subbasin": {}
    }))
    leader.memory.latest_best_by_subbasin = dict(state.get("leader_latest_best_by_subbasin", {}))

    # Build prompt with token budget
    diag_block = state.get("diagnostics_prompt_block", "")

    # Use ContextManager for token budget
    ctx = ContextManager(budget=state.get("leader_token_budget", 50000))

    review_prompt, system_prompt, j_total, outlet_nse, mean_sub_nse, consistency = \
        leader.build_review_prompt(
            round_id=round_id,
            worker_results=worker_results,
            current_center=state["centers"],
            current_radius=state["radii"],
            phase=phase,
            rounds_in_current_phase=state["rounds_in_current_phase"],
            diagnostics_block=diag_block if diag_block.strip() else None,
        )

    # Call LLM (stateless — single-turn, no conversation history)
    print(f"\n  Leader reviewing round {round_id}...")
    decision = None
    leader_tokens = {"prompt": 0, "completion": 0, "total": 0}
    try:
        llm_resp = leader.llm.chat(
            messages=[{"role": "user", "content": review_prompt}],
            system=system_prompt,
        )
        leader_tokens = {
            "prompt": llm_resp.prompt_tokens,
            "completion": llm_resp.completion_tokens,
            "total": llm_resp.total_tokens,
        }
        decision = llm_resp.parsed
        if decision is not None:
            validation = validate_leader_output(decision)
            if not validation.is_valid:
                logger.warning("Leader output validation failed: %s", validation.errors)
                decision = None
            else:
                if "round_id" not in decision:
                    decision["round_id"] = round_id
                if decision.get("stop_or_continue") not in ("continue", "stop"):
                    decision["stop_or_continue"] = "continue"
        if decision is None:
            logger.warning("Leader LLM returned non-JSON. Using fallback.")
    except Exception as e:
        logger.error("Leader LLM call failed: %s. Using fallback.", e)

    if decision is None:
        decision = leader._fallback_decision(
            round_id, worker_results, j_total, outlet_nse, mean_sub_nse, phase=phase,
        )

    # Update leader memory in state
    leader.memory.add_round(
        round_id=round_id,
        worker_results=worker_results,
        j_total=j_total,
        outlet_nse=outlet_nse,
        mean_sub_nse=mean_sub_nse,
        consistency_penalty=consistency,
        leader_decision=decision,
    )

    print(f"  Leader: J_total={j_total:.5f}, outlet_NSE={outlet_nse:.4f}, "
          f"mean_sub_NSE={mean_sub_nse:.4f}")
    print(f"  Strategy: {decision.get('next_round_strategy', 'N/A')}")

    return {
        "leader_decision": decision,
        "leader_memory_rounds": leader.memory.rounds,
        "leader_j_history": leader.memory.j_history,
        "leader_outlet_nse_history": leader.memory.outlet_nse_history,
        "leader_mean_sub_nse_history": leader.memory.mean_sub_nse_history,
        "leader_best_global": leader.memory.best_global,
        "leader_latest_best_by_subbasin": leader.memory.latest_best_by_subbasin,
        "leader_tokens": leader_tokens,
    }


# ---------------------------------------------------------------------------
# 10. update_search_state
# ---------------------------------------------------------------------------

def update_search_state(state: CalibrationState) -> Dict[str, Any]:
    """Update per-basin centers, radii, and direction history."""
    shared = _get_shared(state)
    leader: LeaderAgent = shared["leader"]
    workers = shared["workers"]
    decision = state["leader_decision"]
    worker_results = state["round_worker_results"]
    phase = CalibrationPhase(state["current_phase_name"])
    locked_keys = set(get_locked_keys(phase))
    param_ranges = state["param_ranges"]

    centers = dict(state["centers"])
    radii = dict(state["radii"])
    direction_history = dict(state.get("direction_history", {}))

    result_by_sid = {r["subbasin_id"]: r for r in worker_results}
    for sid in workers:
        result = result_by_sid.get(sid)
        if not result:
            continue

        new_nse = result.get("metrics", {}).get("NSE", float("-inf"))
        worker_best = workers[sid].ledger.get_best_nse()

        if new_nse >= worker_best:
            new_center = normalize_params(
                result.get("best_params", centers[sid]), param_ranges,
            )
            for k in locked_keys:
                new_center[k] = centers[sid].get(k, new_center.get(k, 0))
            centers[sid] = new_center

        # Radius adjustment
        guidance = leader.get_worker_guidance(decision, sid)
        if guidance:
            hint = guidance.get("search_radius_hint", "keep")
            if hint == "shrink":
                radii[sid] = {k: v * 0.8 for k, v in radii[sid].items()}
            elif hint == "expand":
                radii[sid] = {
                    k: min(v * 1.3, (param_ranges.get(k, (0, 1))[1] - param_ranges.get(k, (0, 1))[0]) * 0.5)
                    for k, v in radii[sid].items()
                }

        # Anti-flip direction update
        raw_hints = guidance.get("param_adjustments", []) if guidance else []
        improved = new_nse >= worker_best
        _, direction_history = _apply_anti_flip(direction_history, sid, raw_hints, improved)

    return {
        "centers": centers,
        "radii": radii,
        "direction_history": direction_history,
    }


# ---------------------------------------------------------------------------
# 11. record_round
# ---------------------------------------------------------------------------

def record_round(state: CalibrationState) -> Dict[str, Any]:
    """Record round results: worker memories, ledger, round outputs."""
    shared = _get_shared(state)
    leader: LeaderAgent = shared["leader"]
    workers = shared["workers"]
    decision = state["leader_decision"]
    worker_results = state["round_worker_results"]
    round_id = state["round_id"]
    round_dir = Path(state["round_dir"])
    phase = CalibrationPhase(state["current_phase_name"])

    # Record worker rounds
    worker_memories = dict(state.get("worker_memories", {}))
    for result in worker_results:
        sid = result["subbasin_id"]
        worker = workers.get(sid)
        if not worker:
            continue

        # Restore memory
        wmem_data = worker_memories.get(sid)
        if wmem_data:
            worker.memory = WorkerMemory.from_checkpoint(wmem_data)

        guidance = leader.get_worker_guidance(decision, sid)
        worker.record_round(
            round_id=round_id,
            best_params=result.get("best_params", {}),
            metrics=result.get("metrics", {}),
            score_local=result.get("score_local", 0),
            leader_guidance=guidance,
            analysis=result.get("analysis", ""),
            phase=phase.value,
        )
        worker_memories[sid] = worker.memory.to_checkpoint()

    # Write round outputs
    j_total = state["leader_j_history"][-1] if state.get("leader_j_history") else 0
    outlet_nse = state["leader_outlet_nse_history"][-1] if state.get("leader_outlet_nse_history") else 0
    mean_sub_nse = state["leader_mean_sub_nse_history"][-1] if state.get("leader_mean_sub_nse_history") else 0

    # Worker-to-worker messages
    comm_totals = dict(state.get("comm_totals", {}))
    worker_cfg_by_sid = {w["subbasin_id"]: w for w in state["worker_cfgs"]}
    result_by_sid = {r["subbasin_id"]: r for r in worker_results}

    for w_cfg in state["worker_cfgs"]:
        sid = w_cfg["subbasin_id"]
        for ds in w_cfg.get("neighbors_downstream", []):
            result = result_by_sid.get(sid, {})
            msg = {
                "round_id": round_id,
                "direction": "worker_to_worker",
                "from_subbasin": sid,
                "to_subbasin": ds,
                "upstream_locked_params": result.get("best_params", {}),
            }
            _write_json(round_dir / "messages" / f"{sid}_to_{ds}.json", msg)
            comm_totals["worker_to_worker"] = comm_totals.get("worker_to_worker", 0) + 1

    for result in worker_results:
        sid = result["subbasin_id"]
        _write_json(round_dir / "messages" / f"{sid}_to_leader.json", {
            "round_id": round_id,
            "direction": "worker_to_leader",
            "from_subbasin": sid,
            "best_params": result.get("best_params", {}),
            "metrics": result.get("metrics", {}),
            "score_local": result.get("score_local", 0),
        })
        comm_totals["worker_to_leader"] = comm_totals.get("worker_to_leader", 0) + 1

    # Leader decision markdown
    _write_leader_decision_md(round_dir, round_id, j_total, outlet_nse, mean_sub_nse, decision)

    # Round meta
    meta = {
        "round_id": round_id,
        "phase": phase.value,
        "start_time_utc": state.get("round_start_time", ""),
        "end_time_utc": _utc_now(),
        "j_total": round(j_total, 6),
        "outlet_nse": round(outlet_nse, 4),
        "mean_sub_nse": round(mean_sub_nse, 4),
        "decision": decision.get("stop_or_continue", "continue"),
        "strategy": decision.get("next_round_strategy", ""),
    }
    # Include token usage if available
    leader_tok = state.get("leader_tokens", {})
    if leader_tok and leader_tok.get("total", 0) > 0:
        meta["leader_tokens"] = leader_tok
    # Collect worker tokens from results
    worker_tok_list = []
    for wr in worker_results:
        wt = wr.get("llm_tokens")
        if wt and wt.get("total", 0) > 0:
            worker_tok_list.append({"subbasin": wr["subbasin_id"], **wt})
    if worker_tok_list:
        meta["worker_tokens"] = worker_tok_list
    _write_json(round_dir / "round_meta.json", meta)

    return {
        "worker_memories": worker_memories,
        "comm_totals": comm_totals,
    }


def _write_leader_decision_md(round_dir, round_id, j_total, outlet_nse, mean_sub_nse, decision):
    lines = [
        f"# Round {round_id} Leader Decision\n",
        f"- J_total: {j_total:.5f}",
        f"- Outlet NSE: {outlet_nse:.4f}",
        f"- Mean Sub-basin NSE: {mean_sub_nse:.4f}",
        f"- Trend: {decision.get('global_score_trend', 'N/A')}",
        f"- Decision: {decision.get('stop_or_continue', 'continue')}",
        f"\n## Assessment\n{decision.get('overall_assessment', 'N/A')}",
        f"\n## Strategy\n{decision.get('next_round_strategy', 'N/A')}",
    ]
    with (round_dir / "leader_decision.md").open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 12. check_phase_transition
# ---------------------------------------------------------------------------

def check_phase_transition_node(state: CalibrationState) -> Dict[str, Any]:
    """Check if current phase should transition to next.

    Tracks per-phase plateau detection using mean_sub_nse. If the best
    in-phase mean_sub_nse has not improved for `patience_rounds` consecutive
    rounds, advances the phase early.

    On VIC_ONLY → ROUTING_ONLY transition, cache each basin's VIC fluxes
    so subsequent routing-only rounds can skip the VIC simulation.
    On ROUTING_ONLY → JOINT transition, clear the cache so JOINT rounds
    re-run VIC with the joint-tunable params.
    When the LAST phase ends (patience or max_rounds), schedule one
    mandatory final force-best round via the `force_final_round` flag.
    """
    pipeline = state["phase_pipeline"]
    phase_idx = state["current_phase_idx"]
    rounds_in_phase = state["rounds_in_current_phase"]

    # ── Plateau tracking ─────────────────────────────────────────────
    msn_history = state.get("leader_mean_sub_nse_history", [])
    latest_msn = msn_history[-1] if msn_history else float("-inf")
    best_in_phase = state.get("best_msn_in_phase", float("-inf"))
    rounds_since_improvement = state.get("rounds_since_improvement_in_phase", 0)

    # Suppress patience increment during MC rounds (they explore aggressively
    # and individual rounds rarely beat the cum-best — we let MC complete
    # without forcing transition).
    in_mc_window = False
    cur_phase = pipeline[phase_idx] if phase_idx < len(pipeline) else None
    if cur_phase:
        in_mc_window = rounds_in_phase <= cur_phase.get("mc_rounds", 0)

    if latest_msn > best_in_phase:
        best_in_phase = latest_msn
        rounds_since_improvement = 0
    elif not in_mc_window:
        rounds_since_improvement += 1

    should_trans, next_idx, reason = check_phase_transition(
        pipeline, phase_idx, rounds_in_phase, rounds_since_improvement,
    )

    # Skip transition logic on a final-round invocation (we shouldn't move
    # phases after the user-mandated final force_best round runs).
    if state.get("is_final_round", False):
        return {
            "phase_transitioned": False,
            "best_msn_in_phase": best_in_phase,
            "rounds_since_improvement_in_phase": rounds_since_improvement,
        }

    if should_trans:
        old_name = state["current_phase_name"]
        phase_history = list(state.get("phase_history", []))
        j_hist = state.get("leader_j_history", [])

        # ── Case A: advance to next phase ──
        if next_idx is not None:
            next_phase = pipeline[next_idx]
            new_name = next_phase["name"]
            phase_history.append({
                "from_phase": old_name,
                "to_phase": new_name,
                "at_round": state["round_id"],
                "j_total": j_hist[-1] if j_hist else None,
                "reason": reason,
            })
            print(f"\n  >>> PHASE TRANSITION: {old_name} -> {new_name}")
            print(f"      ({reason})")

            shared = _get_shared(state)
            vic_executor = shared["vic_executor"]
            output_root = Path(state["output_root"])
            if old_name == "VIC_ONLY" and new_name == "ROUTING_ONLY":
                _cache_vic_fluxes_for_routing(
                    vic_executor, state["centers"], output_root, state,
                    state["worker_cfgs"],
                )
            elif old_name == "ROUTING_ONLY" and new_name == "JOINT":
                _clear_vic_flux_cache(vic_executor)

            direction_history = {sid: {} for sid in state.get("direction_history", {})}
            return {
                "current_phase_idx": next_idx,
                "current_phase_name": new_name,
                "rounds_in_current_phase": 0,
                "phase_history": phase_history,
                "phase_transitioned": True,
                "direction_history": direction_history,
                "best_msn_in_phase": float("-inf"),       # reset per new phase
                "rounds_since_improvement_in_phase": 0,
            }

        # ── Case B: last phase ended → schedule final force_best round ──
        phase_history.append({
            "from_phase": old_name,
            "to_phase": "FINAL",
            "at_round": state["round_id"],
            "j_total": j_hist[-1] if j_hist else None,
            "reason": reason,
        })
        print(f"\n  >>> LAST PHASE COMPLETE in {old_name}: {reason}")
        print(f"  >>> Scheduling mandatory FINAL force-best round.")
        return {
            "phase_transitioned": False,
            "force_final_round": True,
            "phase_history": phase_history,
            "best_msn_in_phase": best_in_phase,
            "rounds_since_improvement_in_phase": rounds_since_improvement,
        }

    return {
        "phase_transitioned": False,
        "best_msn_in_phase": best_in_phase,
        "rounds_since_improvement_in_phase": rounds_since_improvement,
    }


def _cache_vic_fluxes_for_routing(
    vic_executor, centers, output_root, state, worker_cfgs,
):
    """Run VIC once per basin with its best soil params, snapshot the
    post-processed flux directory, and point the basin's VICConfig at it
    so subsequent ROUTING_ONLY rounds skip VIC entirely.
    """
    import shutil
    cache_root = output_root / "cached_vic_runoff"
    cache_root.mkdir(parents=True, exist_ok=True)
    print("  >>> Caching VIC fluxes for ROUTING_ONLY phase...")

    for w_cfg in worker_cfgs:
        sid = w_cfg["subbasin_id"]
        vic_cfg = vic_executor._vic_configs.get(sid)
        if vic_cfg is None:
            print(f"      {sid}: no VIC config, skipping cache")
            continue

        best_params = dict(centers.get(sid, {}))
        if not best_params:
            print(f"      {sid}: no best params, skipping cache")
            continue

        # Run VIC + routing with best params (this writes vic_for_routing/)
        up_routing = _build_upstream_routing_info(sid, state, worker_cfgs)
        up_soil = _build_upstream_soil_info(sid, state, worker_cfgs)
        ok, _ = vic_executor.run(
            subbasin_id=sid,
            run_id=99999,
            candidate_id=1,
            params=best_params,
            phase_tag="cache_vic_flux",
            upstream_routing_info=up_routing,
            upstream_soil_info=up_soil,
        )
        if not ok:
            print(f"      {sid}: VIC cache run failed")
            continue

        src = Path(vic_cfg.base_output_dir) / f"{sid}_calibration_ai" / "current_run" / "vic_for_routing"
        if not src.exists():
            print(f"      {sid}: vic_for_routing not found at {src}")
            continue

        dst = cache_root / sid
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(str(src), str(dst))
        vic_cfg.vic_flux_source = str(dst)
        print(f"      {sid}: cached → {dst}")


def _clear_vic_flux_cache(vic_executor):
    """Reset vic_flux_source so VIC runs again during JOINT phase."""
    for sid, vic_cfg in vic_executor._vic_configs.items():
        vic_cfg.vic_flux_source = ""
    print("  >>> Cleared VIC flux cache for JOINT phase")


# ---------------------------------------------------------------------------
# 13. check_round_continuation
# ---------------------------------------------------------------------------

def check_round_continuation(state: CalibrationState) -> Dict[str, Any]:
    """Determine if calibration should continue or stop.

    Stops when EITHER:
      - the round we just ran was the mandatory final force-best round
        (signalled by `is_final_round=True` in state), OR
      - the global max_rounds safety cap was reached.
    """
    round_id = state["round_id"]
    max_rounds = state["max_rounds"]

    if state.get("is_final_round", False):
        return {"should_continue": False}
    if round_id >= max_rounds:
        return {"should_continue": False}

    return {"should_continue": True}


def should_continue(state: CalibrationState) -> str:
    """Conditional edge for round loop."""
    if state.get("should_continue", True):
        return "round_start"
    return "finalize"


# ---------------------------------------------------------------------------
# 14. finalize
# ---------------------------------------------------------------------------

def finalize(state: CalibrationState) -> Dict[str, Any]:
    """Write final summary and convergence outputs."""
    output_root = Path(state["output_root"])
    best = state.get("leader_best_global", {})

    summary = {
        "output_root": str(output_root),
        "mode": state.get("mode", "simulate"),
        "rounds_executed": state.get("round_id", 0),
        "final_phase": state.get("current_phase_name", ""),
        "phase_history": state.get("phase_history", []),
        "best_round": best.get("round_id", -1),
        "best_global_score": best.get("score", float("-inf")),
        "best_params_by_subbasin": best.get("params_by_subbasin", {}),
        "j_history": state.get("leader_j_history", []),
        "outlet_nse_history": state.get("leader_outlet_nse_history", []),
        "communication_totals": state.get("comm_totals", {}),
        "end_time_utc": _utc_now(),
    }
    _write_json(output_root / "final_summary.json", summary)

    print(f"\nCalibration complete. Results: {output_root}")
    print(f"Best round: {best.get('round_id', -1)}, J_total: {best.get('score', 0):.5f}")

    return _to_native({"final_summary": summary})


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------


def needs_exploration(state: CalibrationState) -> str:
    """Conditional edge after check_exploration."""
    cfg = state.get("team_config", {})
    explore_cfg = cfg.get("initial_exploration", {})
    if explore_cfg.get("enabled", False) and state.get("round_id", 0) == 0:
        return "run_exploration"
    return "round_start"
