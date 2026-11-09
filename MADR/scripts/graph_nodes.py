#!/usr/bin/env python3
"""LangGraph orchestration nodes for MADR calibration.

Each node receives calibration state and returns a partial state update."""

from __future__ import annotations
from .experiment_contract import (
    EXPERIMENT_CONTRACT_PROTOCOL, bind_processed_guidance, normalize_decision,
    render_worker_plan, candidate_record, selected_feedback, finalize_feedback, write_contract,
)

import copy
import datetime as dt
import json
import logging
import shutil
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .graph_state import CalibrationState
from .phase_config import (
    load_phase_pipeline,
    check_phase_transition,
    should_use_multi_candidate,
    get_total_max_rounds,
)

from .llm_client import LLMClient, LLMResponse, create_client
from .llm_failure_policy import (bind_policy, validate_policy,
                                 is_historical_fallback, record_historical_recovery, call_usage, summarize_attempt_usage)
from .recovery_accounting import record_worker_recovery_slot, summarize_recovery
from .param_validator import (
    DEFAULT_RANGES,
    CalibrationPhase,
    get_tunable_keys,
    get_locked_keys,
    normalize_params,
    validate_leader_output,
)
from .routing_utils import UpstreamRoutingInfo, find_direc_in_template
from .vic_runner import UpstreamSoilInfo
from .leader_agent import LeaderAgent, LeaderMemory
from .worker_agent import WorkerAgent, WorkerMemory
from .rule_worker_strategy import initial_escape_states
from .vic_executor import VICExecutor, VICConfig
from .leader_coordinator import LeaderCoordinator
from .diagnostic_evidence import validate_mode

from .execution_protocol import candidate_score, runtime_context, validate_periods, is_finite
from .anti_flip import (PROTOCOL as ANTI_FLIP_PROTOCOL, apply_anti_flip as _apply_anti_flip, execution_scope,
                        update_direction_history)

from .execution_consistency import (ExecutionIntegrityError, center_reference, context_identity,
    choose_confirmation, vector_fingerprint, select_accepted, finite_success,
    decision_phase_context, bind_phase_decision, require_guidance_identity)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared state and output utilities
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


# ---------------------------------------------------------------------------
# Shared objects cache (created once per graph invocation)
# ---------------------------------------------------------------------------

_SHARED: Dict[str, Any] = {}


def _get_shared(state: CalibrationState, prepare_cache=True) -> Dict[str, Any]:
    """Lazily build shared objects (leader, workers, vic_executor, etc.)."""
    if _SHARED.get("_initialized"):
        return _SHARED

    cfg = state["team_config"]
    from .execution_protocol import validate_static_input_protocol, validate_worker_evidence_mode
    static_input_protocol = validate_static_input_protocol(cfg.get("static_input_protocol"))
    worker_evidence_mode = validate_worker_evidence_mode(cfg.get("worker_evidence_mode", "existing"))
    from .prompt_visibility import validate_visibility_config
    prompt_visibility_protocol = validate_visibility_config(cfg, method=cfg.get("method", "MADR"))
    from .context_delivery import validate_context_config
    context_delivery_protocol = validate_context_config(cfg, method=cfg.get("method", "MADR"))
    failure_policy = validate_policy(cfg.get("llm_failure_policy", {}))
    if state.get("dry_run", False):
        failure_policy = {"mode": "fallback"}
    diagnostic_mode = validate_mode(cfg.get("diagnostic_protocol", {}).get("mode", "historical_policy"))
    experiment_cfg = cfg.get("experiment_protocol", {})
    if experiment_cfg:
        if experiment_cfg.get("name") != EXPERIMENT_CONTRACT_PROTOCOL:
            raise ValueError("Unsupported experiment protocol")
        if (experiment_cfg.get("radius_policy", "inactive") != "inactive"
                or experiment_cfg.get("anti_flip_policy", ANTI_FLIP_PROTOCOL) != ANTI_FLIP_PROTOCOL
                or experiment_cfg.get("feedback", "selected_candidate") != "selected_candidate"):
            raise ValueError("Unsupported action/feedback policy")
    from .current_evidence import validate as validate_current_evidence
    current_evidence_protocol = validate_current_evidence(cfg.get("current_evidence_protocol"))
    if current_evidence_protocol["enabled"] and context_delivery_protocol["arm"] != "E_refined":
        raise ValueError("Current-reference evidence requires E_refined delivery")
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
        leader_llm = _create_llm_client(leader_cfg, failure_policy, static_input_protocol)
    leader = LeaderAgent(
        agent_id=leader_cfg["agent_id"],
        llm_client=leader_llm,
        workers=worker_cfgs,
        objective_weights=state.get("objective_weights", {}),
        param_ranges=range_lists,
        static_input_protocol=static_input_protocol,
        prompt_visibility_protocol=prompt_visibility_protocol,
        context_delivery_protocol=context_delivery_protocol,
    )

    # Workers
    code_workers = cfg.get("method", "MADR") == "SADR"
    workers: Dict[str, WorkerAgent] = {}
    for w_cfg in worker_cfgs:
        sid = w_cfg["subbasin_id"]
        if code_workers:
            w_llm = None
        elif dry_run:
            w_llm = _DummyLLMClient(f"dry-run-worker-{sid}")
        else:
            w_llm = _create_llm_client(w_cfg, failure_policy, static_input_protocol)
        workers[sid] = WorkerAgent(
            agent_id=w_cfg["agent_id"],
            subbasin_id=sid,
            station_code=w_cfg.get("station_code", sid),
            llm_client=w_llm,
            param_ranges=range_lists,
            window_size=int(w_cfg.get("memory_window", 5)),
            static_input_protocol=static_input_protocol,
            worker_evidence_mode=worker_evidence_mode,
            prompt_visibility_protocol=prompt_visibility_protocol,
            context_delivery_protocol=context_delivery_protocol,
        )
        if code_workers:
            from .rule_worker_strategy import RuleWorkerStrategy
            workers[sid].rule_strategy = RuleWorkerStrategy()
            workers[sid].static_prompt_metadata.update(llm_prompt=False, role="code_worker",
                knowledge_delivery="Shared rule/report provenance; no Worker LLM request")
        workers[sid].set_output_dir(str(output_root))
        workers[sid].diagnostic_mode = diagnostic_mode
        from .current_evidence import configure as configure_current_evidence
        configure_current_evidence(workers[sid], current_evidence_protocol)

    bind_policy(leader, failure_policy, output_root, "Leader")
    for worker in workers.values():
        bind_policy(worker, failure_policy, output_root, "Worker")

    context = runtime_context(cfg)
    leader.factual_runtime_config = cfg
    if context_delivery_protocol['arm'] == 'E_refined':
        from .refined_common import refined_runtime_context
        leader.system_prompt += refined_runtime_context(cfg, 'leader')
    else:
        leader.system_prompt += context
    leader.context_runtime_in_system = True
    for worker in workers.values():
        worker.factual_runtime_config = cfg
        if hasattr(worker, "system_prompt"):
            worker.system_prompt += (refined_runtime_context(cfg, 'worker')
                                     if context_delivery_protocol['arm'] == 'E_refined' else context)
            worker.context_runtime_in_system = True

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
                eval_start_year=validate_periods(cfg)[0],
                eval_end_year=validate_periods(cfg)[1],
                flux_prefix=w_cfg.get("flux_prefix", "fluxes_"),
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

    from .center_reference_cache import validate_config as validate_reference_cache
    reference_cache_config = validate_reference_cache(cfg.get("center_reference_cache"))
    vic_executor.reference_phase = state["current_phase_name"]
    vic_executor.configure_runtime_audit(
        output_root, enabled=cfg.get("runtime_audit", {}).get("enabled", True),
        retain_candidates=cfg.get("runtime_audit", {}).get("retain_candidates", True),
        center_reference_cache=reference_cache_config["enabled"],
    )
    from .input_fingerprint import build_input_fingerprint
    fingerprint = build_input_fingerprint(cfg, vic_executor)
    fingerprint_path = output_root / "provenance" / "input_fingerprint.json"
    fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
    if fingerprint_path.exists():
        previous = json.loads(fingerprint_path.read_text())
        if previous["sha256"] != fingerprint["sha256"]:
            raise ValueError("Resume refused: physical inputs or scoring protocol changed")
    else:
        _write_json(fingerprint_path, fingerprint)

    # Leader coordinator
    upstream_map = state["upstream_map"]
    coordinator = LeaderCoordinator(
        upstream_map=upstream_map,
        outlet_id=cfg["outlet_subbasin_id"],
        diagnostic_mode=diagnostic_mode,
        static_input_protocol=static_input_protocol,
        prompt_visibility_protocol=prompt_visibility_protocol,
    )

    from .run_provenance import AuditedClient
    calls_path = output_root / "llm_calls.jsonl"
    leader.llm = AuditedClient(leader.llm, calls_path, leader.agent_id, source_owner=leader)
    for worker in workers.values():
        if getattr(worker, "llm", None) is not None:
            worker.llm = AuditedClient(worker.llm, calls_path, worker.agent_id, source_owner=worker)

    _SHARED.update({
        "_initialized": True,
        "leader": leader,
        "workers": workers,
        "vic_executor": vic_executor,
        "coordinator": coordinator,
    })
    _SHARED["input_fingerprint"] = fingerprint["sha256"]
    if prepare_cache:
        _configure_phase_cache(state, state["current_phase_name"], executor=vic_executor)
    return _SHARED


def _create_llm_client(agent_cfg: Dict, failure_policy=None, static_input_protocol=None) -> LLMClient:
    policy = validate_policy(failure_policy)
    return create_client(
        model=agent_cfg["model"],
        provider=agent_cfg.get("provider", ""),
        temperature=agent_cfg.get("temperature", 0.3),
        max_tokens=agent_cfg.get("max_tokens", 4096),
        json_mode=True,
        api_key_env=agent_cfg.get("api_key_env", ""),
        timeout_sec=policy.get("timeout_sec", agent_cfg.get("timeout_sec", 120.0)),
        max_retries=policy.get("max_attempts", agent_cfg.get("max_retries", 3)),
        retry_delay_sec=policy.get("retry_delay_sec", agent_cfg.get("retry_delay_sec", 2.0)),
        failure_mode=policy.get("mode", "fallback"),
        base_url=agent_cfg.get("base_url", ""),
        extra_params=agent_cfg.get("extra_params", {}),
        static_input_protocol=static_input_protocol,
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


# ===========================================================================
# NODE FUNCTIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. init_calibration
# ---------------------------------------------------------------------------

def init_calibration(state: CalibrationState) -> Dict[str, Any]:
    """Initialize calibration: load config, create agents, set up state."""
    cfg = state["team_config"]
    from .execution_protocol import validate_worker_evidence_mode
    worker_evidence_mode = validate_worker_evidence_mode(cfg.get("worker_evidence_mode", "existing"))
    from .prompt_visibility import validate_visibility_config
    validate_visibility_config(cfg, method=cfg.get("method", "MADR"))
    from .context_delivery import validate_context_config
    validate_context_config(cfg, method=cfg.get("method", "MADR"))
    from .run_contract import validate_serial, validate_topology
    validate_serial(cfg)
    validate_topology(cfg)
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
    max_rounds = state.get("max_rounds", 0) or cfg.get("max_rounds", 0) or get_total_max_rounds(phase_pipeline)

    # Objective weights
    objective_weights = cfg.get("objective_weights", {"mean_nse": 0.90, "consistency": 0.10})

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
        "worker_evidence_mode": worker_evidence_mode,
        "output_root": str(output_root),
        "mode": state.get("mode", "simulate"),
        "dry_run": bool(state.get("dry_run", False)),
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
        "worker_evidence_mode": worker_evidence_mode,
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
        "max_rounds": max_rounds,
        "max_parallel_workers": int(cfg.get("max_parallel_workers", 4)),
        "centers": centers,
        "accepted_center_records": {},
        "last_center_acceptance": {},
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
        "worker_memories": worker_memories,
        "comm_totals": {"leader_to_worker": 0, "worker_to_leader": 0, "worker_to_worker": 0},
        "mc_round_summaries": {},
        "escape_states": (initial_escape_states(worker_cfgs)
                          if cfg.get("method", "MADR") == "SADR" else {}),
        "exploration_results": {},
        "exploration_summary": "",
        "should_continue": True,
    }


# ---------------------------------------------------------------------------
# 2. check_exploration / run_exploration
# ---------------------------------------------------------------------------

def check_exploration(state: CalibrationState) -> Dict[str, Any]:
    """Check if initial exploration is needed (pass-through node)."""
    return {}


def run_exploration(state: CalibrationState) -> Dict[str, Any]:
    """Run initial exploration (pure code, zero LLM calls)."""
    from .initial_explorer import format_exploration_summary

    shared = _get_shared(state)
    cfg = state["team_config"]
    output_root = Path(state["output_root"])

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

    from .exploration_reuse import run_exploration_protocol
    results = run_exploration_protocol(adapter, cfg, output_root)
    summary = format_exploration_summary(results)
    if cfg.get("diagnostic_protocol", {}).get("mode") == "historical_policy":
        from .historical_policy import policy_exploration_summary
        summary = policy_exploration_summary(results, cfg.get("static_input_protocol"))

    # Save summary
    explore_out = {
        sid: {"best_nse": r["best_nse"], "best_params": r["best_params"],
              "n_samples": r["n_samples"], "summary": r["summary_text"]}
        for sid, r in results.items()
    }
    _write_json(output_root / "initial_exploration.json", explore_out)

    # Save full per-basin exploration results (all configured exploration samples)
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
    """Begin a new calibration round."""
    round_id = state.get("round_id", 0) + 1
    max_rounds = state["max_rounds"]
    pipeline = state["phase_pipeline"]
    phase_idx = state["current_phase_idx"]
    final_phase_cap = (phase_idx == len(pipeline) - 1 and
                       state["rounds_in_current_phase"] + 1 >= pipeline[phase_idx]["max_rounds"])
    is_final = (round_id >= max_rounds or final_phase_cap or state.get("force_final_round", False))
    phase_name = state["current_phase_name"]

    print(f"\n{'='*60}")
    print(f"  ROUND {round_id}/{max_rounds}  |  Phase: {phase_name}")
    if is_final:
        print(f"  ★ FINAL ROUND — confirming one highest-mean accepted joint vector")
    print(f"{'='*60}\n")

    confirmation = {}
    if is_final:
        receipt = choose_confirmation(state["output_root"], round_id,
            [w["subbasin_id"] for w in state["worker_cfgs"]])
        confirmation = {"confirmation_vector": receipt["params_by_subbasin"], "confirmation_receipt": receipt}

    # Round directory
    output_root = Path(state["output_root"])
    round_dir = output_root / f"round_{round_id:03d}"
    for subdir in ("tasks", "results", "messages"):
        _ensure_dir(round_dir / subdir)

    return {
        "round_id": round_id,
        "is_final_round": is_final,
        **confirmation,
        "round_start_time": _utc_now(),
        "round_dir": str(round_dir),
        "rounds_in_current_phase": state.get("rounds_in_current_phase", 0) + 1,
        "current_level_idx": 0,
        "round_worker_results": [],
        "batch_worker_results": [],
        "mc_round_summaries": dict(state.get("mc_round_summaries", {})),
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
    """Evaluate the current topological batch using the configured concurrency limit."""
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
        worker.factual_runtime_progress = {
            "rounds_in_current_phase": state.get("rounds_in_current_phase"),
            "phase_history": list(state.get("phase_history", [])),
        }
        center = (state["confirmation_vector"] if force_best else state["centers"])[sid]
        radius = state["radii"][sid]

        # Restore worker memory
        wmem_data = state.get("worker_memories", {}).get(sid)
        if wmem_data:
            worker.memory = WorkerMemory.from_checkpoint(wmem_data)

        # Inject per-basin exploration summary for Worker prompt
        full_explore = state.get("exploration_summary", "")
        if full_explore:
            # Extract this basin's line from the full summary
            basin_lines = [ln for ln in full_explore.split("\n") if sid in ln]
            worker._exploration_summary = "\n".join(basin_lines) if basin_lines else ""
        else:
            worker._exploration_summary = ""

        # Leader guidance
        prev_decision = state.get("leader_decision")
        guidance = None
        if prev_decision:
            guidance = shared["leader"].get_worker_guidance(prev_decision, sid)
        require_guidance_identity(state, prev_decision, guidance, sid)

        # Current execution scope must be known before reading direction evidence.
        execution_state = dict(state, centers=state["confirmation_vector"]) if force_best else state
        up_routing_info = _build_upstream_routing_info(sid, execution_state, worker_cfgs)
        up_soil_info = _build_upstream_soil_info(sid, execution_state, worker_cfgs)
        context, context_id = context_identity(up_soil_info, up_routing_info)
        previous_accepted = copy.deepcopy(state.get("accepted_center_records", {}).get(sid))
        reference = center_reference(state, worker, vic_executor, center, up_soil_info, up_routing_info)

        # Pure filtering; only completed observed experiments update the history.
        raw_hints = guidance.get("param_adjustments", []) if guidance else []
        scope = execution_scope(phase.value, context_id, reference, shared.get("input_fingerprint"))
        filtered_hints, anti_flip_audit = _apply_anti_flip(
            state.get("direction_history", {}), sid, raw_hints, scope)
        if guidance:
            guidance = bind_processed_guidance(guidance, filtered_hints)

        from .attribution_evidence import before_proposal, write_evidence
        attribution_before = before_proposal(
            round_id=round_id, sid=sid, phase=phase.value, previous=previous_accepted, reference=reference,
            input_fingerprint=shared.get("input_fingerprint"), mode=state.get("mode"), terminal=force_best)
        worker.current_attribution_evidence = attribution_before
        write_evidence(round_dir, sid, "before_proposal", attribution_before)
        worker.current_reference_record = reference
        worker.factual_metric_record = reference
        from .current_evidence import prepare as prepare_current_evidence
        prepare_current_evidence(worker, state, reference, attribution_before)
        if guidance:
            guidance = dict(guidance, current_center=dict(center))

        if hasattr(worker, "rule_strategy"):
            worker.rule_strategy.configure(phase=phase.value,
                local_round=state["rounds_in_current_phase"],
                escape_state=state.get("escape_states", {}).get(sid, {}), guidance=guidance)

        def attach_execution(result):
            context, context_id = context_identity(up_soil_info, up_routing_info)
            saved = json.loads((round_dir / "results" / f"worker_{sid}.json").read_text())
            cid = saved.get("winning_candidate_id", 1)
            detail = next((r for r in saved.get("mc_detail", []) if r["candidate_id"] == cid), None)
            tag = f"round_{round_id}_mc{cid}_{detail['label']}" if detail else f"round_{round_id}_c1"
            routing = round_dir / "routing_result" / sid
            if detail:
                routing = routing / f"c{cid}_{detail['label']}"
            result["attribution_before_proposal"] = copy.deepcopy(attribution_before)
            result.update(center_reference=reference, upstream_context=context, upstream_context_id=context_id,
                          executed_round_id=round_id, execution_identity={"run_id":round_id*100+cid,"candidate_id":cid,"phase_tag":tag},
                          routing_result_relative=str(routing.relative_to(Path(state["output_root"]))),
                          consumed_guidance=copy.deepcopy(guidance),
                          anti_flip_audit=copy.deepcopy(anti_flip_audit),
                          consumed_guidance_round_id=(prev_decision or {}).get("round_id"),
                          executed_phase=phase.value,
                          consumed_guidance_identity=copy.deepcopy((prev_decision or {}).get("_guidance_identity")))
            return result

        # R1 (no leader decision yet) → exact defaults, skip MC even if enabled
        if use_multi and prev_decision is not None:
            result, mc_line = _run_worker_multi(
                worker, vic_executor, round_id, center, radius,
                guidance, round_dir, state.get("mc_n_candidates", 5),
                phase, up_routing_info, up_soil_info,
            )
            return attach_execution(result), mc_line
        else:
            result = _run_worker_single(
                worker, vic_executor, round_id, center, radius,
                guidance, round_dir, phase, up_routing_info, up_soil_info,
                force_best=force_best,
            )
            return attach_execution(result), ""

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
            except ExecutionIntegrityError:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as e:
                logger.error("Worker %s failed: %s\n%s", sid, e, traceback.format_exc())
                for pending in futures:
                    pending.cancel()
                raise

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
    from .process_trace import capture_snapshot, trace_worker_candidate
    sid = worker.subbasin_id
    t0 = dt.datetime.now(dt.timezone.utc)
    locked_keys = set(get_locked_keys(phase))
    phase_str = phase.value
    worker_tokens = {"prompt": 0, "completion": 0, "total": 0}
    proposal_snapshot = None
    trace_reasoning = ""
    trace_context = ""
    trace_branch = "force_best" if force_best else "exact_center_no_guidance"

    if force_best:
        candidate_list = [dict(center)]
    elif guidance is not None and hasattr(worker, "rule_strategy"):
        candidate_list, proposal_snapshot = worker.rule_strategy.single(worker, center, phase_str, radius)
        trace_reasoning = proposal_snapshot["reasoning"]
        trace_context = render_worker_plan(guidance, phase=phase_str,
            static_input_protocol=getattr(worker,"static_input_protocol",None),
            context_delivery_protocol=getattr(worker,"context_delivery_protocol",None))
        trace_branch = proposal_snapshot["branch"]
        from .process_trace import record_worker_proposal
        record_worker_proposal(worker, proposal_snapshot)
    elif guidance is not None:
        # A valid empty action list is still a Leader plan. The MADR Worker
        # reviews local evidence and supplies this scheduled proposal (possibly
        # an explicit hold), as it already does in multi-candidate rounds.
        leader_context = render_worker_plan(guidance, phase=phase_str, static_input_protocol=getattr(worker, "static_input_protocol", None), context_delivery_protocol=getattr(worker, "context_delivery_protocol", None))

        # Pass the measured current-center metrics in the actual upstream background
        best_record = getattr(worker, "current_reference_record", None)
        worker.factual_metric_record = best_record
        best_metrics = best_record.get("metrics", {}) if best_record else {}

        candidate_list, reasoning, worker_tokens = worker.propose_params(
            round_id=round_id,
            current_center=center,
            metrics=best_metrics,
            phase=phase_str,
            leader_context=leader_context,
        )
        proposal_snapshot = capture_snapshot(getattr(worker, "last_proposal_trace", None), round_dir)
        trace_reasoning, trace_context = reasoning, leader_context
        trace_branch = (proposal_snapshot or {}).get("branch", "worker_proposal")
    else:
        # Round 1 (no guidance): use exact default params with zero noise
        candidate_list = [dict(center)]

    candidate_before_locks = capture_snapshot(candidate_list[0], round_dir)
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
    score = candidate_score(ok, metrics)
    duration = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()

    record_worker_recovery_slot(worker, round_id=round_id, phase=phase_str, candidate_id=1,
        branch=trace_branch, proposal=proposal_snapshot,
        reason=(proposal_snapshot or {}).get("fallback_reason"), ok=ok)
    trace_worker_candidate(
        round_dir, round_id, worker, params, metrics, ok, phase=phase_str,
        branch=trace_branch, candidate_before_locks=candidate_before_locks,
        reasoning=trace_reasoning, leader_context=trace_context,
        fallback_reason=(proposal_snapshot or {}).get("fallback_reason"),
        proposal_trace=proposal_snapshot, guidance=guidance,
    )

    experiment = candidate_record(
        round_id=round_id, sid=sid, candidate_number=1, phase=phase_str,
        branch=trace_branch, guidance=guidance, proposal=proposal_snapshot,
        before_locks=candidate_before_locks, effective_params=params, metrics=metrics, ok=ok,
        trial_source="Leader" if hasattr(worker,"rule_strategy") else "Worker", reasoning=trace_reasoning, worker_context=trace_context,
        history_feedbacks=getattr(getattr(worker, "memory", None), "history", []),
        base_record=getattr(worker, "current_reference_record", None), up_soil=up_soil, up_routing=up_routing,
    )
    write_contract(round_dir, f"{sid}_c1", experiment)

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
        "ok": ok,
        "experiment_feedback": selected_feedback(experiment),
        "analysis": trace_reasoning if hasattr(worker,"rule_strategy") else "1-candidate run.",
        "llm_tokens": worker_tokens,
        "duration_sec": round(duration, 3),
    }


def _run_worker_multi(
    worker, vic_executor, round_id, center, radius, guidance,
    round_dir, n_candidates, phase, up_routing, up_soil,
) -> Tuple[Dict, str]:
    """Evaluate a configured set of LLM-proposed candidates.

Each slot requests its assigned proposal strategy. Unusable proposals follow
the configured failure policy. The executor enforces phase constraints and
selects the best valid result; feedback retains effective parameters and ties."""
    from .process_trace import capture_snapshot, trace_worker_candidate
    MC_STRATEGIES = ["conservative", "moderate", "bold"]
    if isinstance(n_candidates, bool) or not isinstance(n_candidates, int) or not 1 <= n_candidates <= len(MC_STRATEGIES):
        raise ValueError("MADR MC n_candidates must be an integer from 1 to 3")

    sid = worker.subbasin_id
    t0 = dt.datetime.now(dt.timezone.utc)
    locked_keys = set(get_locked_keys(phase))
    phase_str = phase.value
    total_tokens = {"prompt": 0, "completion": 0, "total": 0, "unknown_usage_attempts": 0}

    # ── Step 1: Build leader context ──
    leader_context = render_worker_plan(guidance, phase=phase_str, static_input_protocol=getattr(worker, "static_input_protocol", None), context_delivery_protocol=getattr(worker, "context_delivery_protocol", None))

    # Historical best metrics for LLM context
    best_record = getattr(worker, "current_reference_record", None)
    worker.factual_metric_record = best_record
    best_metrics = best_record.get("metrics", {}) if best_record else {}

    # ── Step 2: Call Worker LLM n_candidates times with different strategies ──
    strategies = MC_STRATEGIES[:n_candidates]
    candidates = []
    candidate_traces = []

    def locked_params(candidate):
        params = dict(candidate)
        for key in locked_keys:
            params[key] = center[key]
        return normalize_params(params, worker.ranges)

    if hasattr(worker, "rule_strategy"):
        generated = worker.rule_strategy.multi(worker, center, phase_str, n_candidates)
        strategies = []
        for candidate, label, proposal in generated:
            candidates.append((candidate, label))
            candidate_traces.append({"proposal_trace": proposal, "reasoning": proposal["reasoning"],
                "leader_context": leader_context, "source": proposal["source"],
                "branch": proposal["branch"], "fallback_reason": None, "slot_recovery": None})

    for strategy in strategies:
        from .prompt_source import get_text, render_static
        source_protocol = getattr(worker, "static_input_protocol", None)
        section = "graph_mc_" + strategy
        strategy_context = render_static("graph_mc_strategy", protocol=source_protocol,
            leader_context=leader_context, strategy=strategy,
            instruction=get_text(section, protocol=source_protocol))

        # Only a newly produced trace belongs to this slot. A call that fails
        # before entering WorkerAgent must not inherit the preceding call_id.
        prior_trace = getattr(worker, "last_proposal_trace", None)
        prior_trace_ids = (prior_trace.get("call_id"), prior_trace.get("proposal_id")) if isinstance(prior_trace, dict) else None
        reasoning = ""
        failure_reason = None
        error_type = None
        cand = None
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
                # Validate execution after phase locks without consuming RNG or
                # changing the original proposal preserved below for auditing.
                failure_reason = "invalid_worker_proposal"
                locked_params(cand)
                failure_reason = None
            else:
                failure_reason = "empty_worker_proposal"
        except ExecutionIntegrityError:
            raise
        except Exception as e:
            failure_reason = failure_reason or "worker_proposal_exception"
            error_type = type(e).__name__

        current_trace = getattr(worker, "last_proposal_trace", None)
        current_trace_ids = (current_trace.get("call_id"), current_trace.get("proposal_id")) if isinstance(current_trace, dict) else None
        fresh_trace = current_trace is not prior_trace or current_trace_ids != prior_trace_ids
        # Source labels enter the next Leader context. Read live call metadata,
        # not the archival copy: a failed recorder must not change that feedback.
        proposal = current_trace if fresh_trace and isinstance(current_trace, dict) else {}
        branch = proposal.get("branch", "worker_proposal_unclassified")
        fallback_reason = proposal.get("fallback_reason")
        proposal_snapshot = capture_snapshot(current_trace, round_dir) if fresh_trace else None
        source = "llm" if branch == "llm_proposal" else (
            "historical_rule_fallback" if branch == "historical_rule_fallback" else
            "neutral_fallback" if branch == "neutral_fallback" else "worker_proposal_unclassified")
        recovery = None
        if failure_reason is not None:
            # Ordinary proposal failures consume a slot, never remove it. The
            # configured helper supplies a recorded replacement for this slot.
            # Process-control BaseExceptions deliberately propagate.
            recovery = {"proposal_failure_reason": failure_reason,
                        "proposal_error_type": error_type}
            fallback_reason = failure_reason
            logger.warning("Worker %s MC-%s recovering slot: %s (%s)",
                           sid, strategy, failure_reason, error_type or "no_exception")
            try:
                fallback_list = worker._fallback_execute(center, phase_str)
                if not fallback_list:
                    recovery["fallback_failure_reason"] = "empty_neutral_fallback"
                    raise ValueError("empty neutral fallback")
                cand = fallback_list[0]
                recovery["fallback_failure_reason"] = "invalid_neutral_fallback"
                locked_params(cand)
                recovery.pop("fallback_failure_reason", None)
                historical = is_historical_fallback(worker)
                branch = "historical_rule_fallback" if historical else "graph_neutral_fallback"
                source = "historical_rule_fallback" if historical else "neutral_fallback"
                reasoning = ("Historical code fallback fills this MC slot with diagnosis-based steps and perturbations."
                             if historical else "Neutral failure fallback repeats the accepted center for this MC strategy slot.")
            except ExecutionIntegrityError:
                raise
            except Exception as e:
                recovery.setdefault("fallback_failure_reason", "neutral_fallback_exception")
                recovery["fallback_error_type"] = type(e).__name__
                # Repeated centers are allowed and are still evaluated in each
                # slot. If even this center is not executable, fail explicitly.
                cand = dict(center)
                try:
                    locked_params(cand)
                except Exception:
                    raise RuntimeError("MADR MC slot recovery has no executable center") from None
                branch = "graph_center_fallback"
                source = "center_fallback"
                reasoning = "The neutral fallback was unavailable; the current center fills this MC strategy slot."
                logger.warning("Worker %s MC-%s using center: %s (%s)",
                               sid, strategy, recovery["fallback_failure_reason"], type(e).__name__)

        candidates.append((cand, strategy))
        candidate_traces.append({"proposal_trace": proposal_snapshot,
                                 "reasoning": reasoning, "leader_context": strategy_context,
                                 "source": source, "branch": branch,
                                 "fallback_reason": fallback_reason, "slot_recovery": recovery})
        logger.info("Worker %s MC-%s candidate source: %s", sid, strategy, source)

    # ── Step 3: Evaluate all candidates ──
    eval_results = []
    vic_cfg = vic_executor._vic_configs.get(sid)
    for i, (params, label) in enumerate(candidates):
        before_locks = capture_snapshot(params, round_dir)
        params = locked_params(params)
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
        score = candidate_score(ok, metrics)
        trace = candidate_traces[i]
        eval_results.append({
            "idx": i, "params": params, "metrics": metrics,
            "ok": ok, "score": score, "nse": nse, "label": label,
            "source": trace["source"], "branch": trace["branch"],
            "fallback_reason": trace["fallback_reason"], "slot_recovery": trace["slot_recovery"],
        })
        experiment = candidate_record(
            round_id=round_id, sid=sid, candidate_number=i + 1, phase=phase_str,
            branch=trace["branch"], guidance=guidance, proposal=trace["proposal_trace"],
            before_locks=before_locks, effective_params=params, metrics=metrics, ok=ok,
            trial_source="Leader" if hasattr(worker,"rule_strategy") else "Worker", reasoning=trace["reasoning"], worker_context=trace["leader_context"],
            history_feedbacks=getattr(getattr(worker, "memory", None), "history", []),
            base_record=best_record, up_soil=up_soil, up_routing=up_routing,
            execution_details={"strategy": label, "slot_recovery": trace["slot_recovery"]},
        )
        eval_results[-1]["experiment"] = experiment
        write_contract(round_dir, f"{sid}_c{i + 1}", experiment)
        record_worker_recovery_slot(worker, round_id=round_id, phase=phase_str, candidate_id=i + 1,
            branch=trace["branch"], proposal=trace["proposal_trace"],
            slot_recovery=trace["slot_recovery"], reason=trace["fallback_reason"], ok=ok)
        trace_worker_candidate(
            round_dir, round_id, worker, params, metrics, ok, candidate_id=i + 1,
            label=label, phase=phase_str, candidate_before_locks=before_locks,
            branch=trace["branch"], fallback_reason=trace["fallback_reason"],
            proposal_trace=trace["proposal_trace"], reasoning=trace["reasoning"],
            leader_context=trace["leader_context"], guidance=guidance,
            candidate_source=trace["source"], slot_recovery=trace["slot_recovery"],
            proposal_trace_role="attempted_proposal" if trace["slot_recovery"] else "selected_proposal",
        )
        print(f"    [{sid}] MC candidate {i+1}/{len(candidates)} ({label}): NSE={nse:.4f}")

    eval_results.sort(key=lambda x: x["score"], reverse=True)
    best = eval_results[0]

    step_parts = [f"{r['label']}" + (f"[{r['source']}]" if r['source'] != "llm" else "")
                  + f"→{r['nse']:.3f}" for r in eval_results]
    from .phase_mc_feedback import format_mc_summary
    selection_text = format_mc_summary(eval_results, best)
    summary_line = f"{sid}: {selection_text}, " + ", ".join(step_parts)
    sources = {trace["source"] for trace in candidate_traces}
    source_summary = "LLM" if sources == {"llm"} else (
        "historical_rule_fallback" if sources == {"historical_rule_fallback"} else
        "neutral_fallback" if sources <= {"neutral_fallback", "center_fallback"} else (
            "unclassified" if sources == {"worker_proposal_unclassified"} else "mixed"))

    if hasattr(worker, "rule_strategy"):
        source_summary = "escape_rule" if worker.rule_strategy.escape else "rule_worker"

    duration = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()

    result = {
        "subbasin_id": sid,
        "station_code": worker.station_code,
        "agent_id": worker.agent_id,
        "best_params": best["params"],
        "params": best["params"],
        "metrics": best["metrics"],
        "score_local": round(best["score"], 6),
        "ok": best["ok"],
        "experiment_feedback": selected_feedback(best["experiment"], [r["experiment"] for r in sorted(eval_results, key=lambda r: r["idx"])]),
        "analysis": f"MC({len(candidates)},{source_summary}): {selection_text}",
        "llm_tokens": total_tokens,
        "duration_sec": round(duration, 3),
    }

    _ensure_dir(round_dir / "results")
    _write_json(round_dir / "results" / f"worker_{sid}.json", {
        "round_id": round_id, "subbasin_id": sid,
        "result": {"params": best["params"], "metrics": best["metrics"],
                   "ok": best["ok"], "score_local": round(best["score"], 6)},
        "winning_candidate_id": best["idx"] + 1,
        "mc_detail": [{"candidate_id": r["idx"] + 1, "label": r["label"], "nse": r["nse"], "score": r["score"], "params": r["params"], "ok": r["ok"], "metrics": r["metrics"],
                       "source": r["source"], "branch": r["branch"], "fallback_reason": r["fallback_reason"], "slot_recovery": r["slot_recovery"]}
                      for r in eval_results],
        "llm_tokens": total_tokens,
        "duration_sec": round(duration, 3),
    })

    return result, summary_line


# ---------------------------------------------------------------------------
# 6. collect_batch_results
# ---------------------------------------------------------------------------

def collect_batch_results(state: CalibrationState) -> Dict[str, Any]:
    """Keep proposals separate from the evaluated accepted joint state."""
    centers=copy.deepcopy(state["centers"])
    records=copy.deepcopy(state.get("accepted_center_records",{}))
    flags=dict(state.get("last_center_acceptance",{}))
    rows=list(state.get("round_worker_results",[]))
    latest=copy.deepcopy(state.get("leader_latest_best_by_subbasin",{}))
    escape_states=copy.deepcopy(state.get("escape_states",{}))
    for proposal in state.get("batch_worker_results",[]):
        sid=proposal["subbasin_id"]
        accepted,record,reason=select_accepted(proposal,centers[sid],records.get(sid),state.get("is_final_round",False))
        if sid in escape_states:
            from .rule_worker_strategy import update_escape_state
            escape_receipt=update_escape_state(escape_states[sid], candidate=proposal,
                reference=proposal.get("center_reference"), current_center=centers[sid],
                accepted=accepted, round_id=state["round_id"], phase=state["current_phase_name"],
                terminal=state.get("is_final_round",False))
            escape_trace=Path(state["round_dir"])/"process_trace"/f"{sid}_escape.json"
            _ensure_dir(escape_trace.parent)
            _write_json(escape_trace, escape_receipt)
        from .attribution_evidence import before_proposal, completed_round, write_evidence
        before = proposal.get("attribution_before_proposal")
        if before is None:
            before = before_proposal(round_id=state["round_id"], sid=sid,
                phase=state.get("current_phase_name"), previous=records.get(sid),
                reference=proposal.get("center_reference"), terminal=state.get("is_final_round", False))
        attribution = completed_round(before, candidate=proposal, accepted_record=record,
                                      accepted=accepted, reason=reason)
        write_evidence(state["round_dir"], sid, "completed", attribution)
        records[sid]=record
        flags[sid]=accepted
        if not state.get("is_final_round"):
            centers[sid]=dict(record["params"])
        feedback=proposal.get("experiment_feedback")
        finalize_feedback(feedback,accepted=accepted,center=record["params"],reason=reason,
                          static_input_protocol=state.get("team_config", {}).get("static_input_protocol"))
        if feedback:
            feedback["center_reference"]=copy.deepcopy(proposal.get("center_reference"))
            feedback["historical_best_is_acceptance_threshold"]=False
            write_contract(state["round_dir"],f"{sid}_selected",feedback)
        if state.get("is_final_round"):
            accepted_analysis = "Terminal confirmation of one frozen joint parameter vector; not a new search proposal."
        elif accepted:
            accepted_analysis = proposal.get("analysis", "") + " Candidate accepted against the same-background center."
        elif proposal.get("ok") is not True:
            accepted_analysis = "Retained the measured same-background center after an invalid candidate; no proposal was accepted."
        else:
            accepted_analysis = "Retained the measured same-background center; the new candidate was rejected by the NSE comparison."
        result=dict(proposal)
        result["attribution_evidence"] = attribution
        result["analysis"] = accepted_analysis
        result.update(params=record["params"],best_params=record["params"],metrics=record["metrics"],ok=True,
                      score_local=candidate_score(True,record["metrics"]),accepted_as_center=accepted,
                      execution_identity=record["execution_identity"],routing_result_relative=record["routing_result_relative"],
                      upstream_context=record["upstream_context"],upstream_context_id=record["upstream_context_id"],
                      proposal_result=copy.deepcopy(proposal),accepted_record_source=record["source"])
        rows.append(result)
        latest[sid]={"round_id":state["round_id"],"params":record["params"],"metrics":record["metrics"],
                     "scope":"current_accepted_center_same_background"}
        path=Path(state["round_dir"])/"results"/f"worker_{sid}.json"
        saved=json.loads(path.read_text()) if path.exists() else {"round_id":state["round_id"],"subbasin_id":sid}
        saved["proposal_result"]=saved.get("result",{})
        saved["proposal_result"]["analysis"]=proposal.get("analysis", "")
        saved["analysis"]=accepted_analysis
        saved["attribution_evidence_relative"] = f"round_{state['round_id']:03d}/attribution/{sid}_completed.json"
        saved["proposal_winning_candidate_id"]=saved.get("winning_candidate_id",1)
        saved["result"]={"params":record["params"],"metrics":record["metrics"],"ok":True,"score_local":result["score_local"]}
        saved.update(result_scope="accepted_joint_state",execution_identity=record["execution_identity"],
                     routing_result_relative=record["routing_result_relative"],
                     winning_candidate_id=record["execution_identity"]["candidate_id"],
                     upstream_context=record["upstream_context"],upstream_context_id=record["upstream_context_id"],
                     accepted_as_center=accepted,acceptance_reason=reason)
        _write_json(path,saved)
        _ensure_dir(Path(state["round_dir"])/"center_references")
        _write_json(Path(state["round_dir"])/"center_references"/f"{sid}_acceptance.json",{
            "round_id":state["round_id"],"accepted":accepted,"reason":reason,"candidate":proposal,
            "accepted_state":record})
    return _to_native({"round_worker_results":rows,"centers":centers,"accepted_center_records":records,
                       "last_center_acceptance":flags,"leader_latest_best_by_subbasin":latest,"escape_states":escape_states,"batch_worker_results":[]})


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
    phase_context = decision_phase_context(state)
    decision_phase = CalibrationPhase(phase_context["decision_target_phase"])

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
        upstream_quality = "unknown"
        upstreams = [result_by_sid.get(u, {}) for u in upstream_map.get(sid, [])]
        if upstreams and all(r.get("ok") is True and is_finite(r.get("metrics", {}).get("NSE")) for r in upstreams):
            upstream_quality = "poor" if any(r["metrics"]["NSE"] < 0.5 for r in upstreams) else "good"

        report = worker.report_diagnostics(
            metrics=result.get("metrics", {}),
            prev_metrics=prev_metrics,
            context={"is_headwater": is_headwater, "upstream_quality": upstream_quality,
                     "simulation_ok": result.get("ok") is True,
                     "round_id": state["round_id"],
                     "evaluated_params": result.get("best_params", result.get("params")),
                     "observed_phase": state["current_phase_name"],
                     "accepted_evaluation_record": result},
        )
        report["simulation_ok"] = result.get("ok") is True
        report["evaluated_params"] = result.get("best_params", result.get("params"))
        report["accepted_center_after_round"] = state.get("centers", {}).get(sid)
        report["diagnostic_mode"] = coordinator.diagnostic_mode
        report["experiment_feedback"] = result.get("experiment_feedback")
        diagnostic_reports.append(report)

    # Cross-basin insights
    coordinator_summaries = []
    for report in diagnostic_reports:
        diag = report.get("diagnosis", {})
        ledger = report.get("ledger_summary", {})
        coordinator_summaries.append({
            "subbasin_id": report.get("subbasin_id"),
            "current_nse": report.get("current_metrics", {}).get("NSE"),
            "simulation_ok": report.get("simulation_ok"),
            "primary_issue": diag.get("primary_issue", "none"),
            "improvement_trend": diag.get("improvement_trend", "?"),
            "stagnation_risk": ledger.get("stagnation_count", 0) >= 3,
            "stagnation_count": ledger.get("stagnation_count", 0),
            "metrics": report.get("current_metrics", {}),
            "issue_status": diag.get("issue_status", {}),
            **({"top_recommendations": [f"{r['param']} {r['direction']}"
                 for r in diag.get("recommendations", [])[:3]],
                "suggested_direction": diag.get("reasoning", ""),
                "routing_responsibility": diag.get("routing_responsibility", 0)}
               if coordinator.diagnostic_mode == "historical_policy" else {}),
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
        active_params=get_tunable_keys(decision_phase),
    )

    # Inject exploration summary for round 1
    diag_block = diagnostics_prompt_block or ""
    if round_id <= 1 and state.get("exploration_summary", ""):
        diag_block = state["exploration_summary"] + "\n\n" + diag_block

    # Inject MC summary
    mc_summary = state.get("mc_round_summaries", {}).get(round_id, "")
    if mc_summary:
        diag_block += "\n\n### Multi-Candidate Step-Size Response\n" + mc_summary

    return {
        "diagnostic_reports": diagnostic_reports,
        "diagnostics_prompt_block": diag_block,
        "coordination_result": coordination_result.to_dict() if hasattr(coordination_result, 'to_dict') else {},
    }


# ---------------------------------------------------------------------------
# 9. leader_review (stateless — no conversation accumulation)
# ---------------------------------------------------------------------------

def leader_review(state: CalibrationState) -> Dict[str, Any]:
    """Have Leader LLM review the round. Stateless — no conversation history."""
    from .process_trace import write_round_trace, write_existing_mc_diagnostics
    shared = _get_shared(state)
    leader: LeaderAgent = shared["leader"]
    round_id = state["round_id"]
    worker_results = state["round_worker_results"]
    phase = CalibrationPhase(state["current_phase_name"])
    phase_context = decision_phase_context(state)
    decision_phase = CalibrationPhase(phase_context["decision_target_phase"])

    vector = {row["subbasin_id"]: row.get("best_params") for row in worker_results}
    expected_stations = {w["subbasin_id"] for w in state["worker_cfgs"]}
    if set(vector) != expected_stations or len(vector) != len(worker_results):
        raise ExecutionIntegrityError("Global scoring requires exactly all stations")
    execution_state = dict(state, centers=vector)
    for row in worker_results:
        sid = row["subbasin_id"]
        _, expected_context_id = context_identity(
            _build_upstream_soil_info(sid, execution_state, state["worker_cfgs"]),
            _build_upstream_routing_info(sid, execution_state, state["worker_cfgs"]))
        if (row.get("ok") is not True or not is_finite(row.get("metrics", {}).get("NSE"))
                or row.get("upstream_context_id") != expected_context_id):
            raise ExecutionIntegrityError(f"{sid}: refuse scoring mismatched/nonfinite joint execution")

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

    leader.factual_runtime_progress = {
        **phase_context,
        "rounds_in_current_phase": state.get("rounds_in_current_phase"),
        "phase_history": list(state.get("phase_history", [])),
    }
    # Raw, archived sources for the compact projection; no new diagnosis or rule.
    leader.context_diagnostic_reports = state.get("diagnostic_reports", [])
    leader.context_coordination_result = state.get("coordination_result", {})
    leader.context_exploration = state.get("exploration_summary", "") if round_id <= 1 else ""
    leader.context_mc_summary = state.get("mc_round_summaries", {}).get(round_id, "")
    # Build prompt with token budget
    diag_block = state.get("diagnostics_prompt_block", "")

    review_prompt, system_prompt, j_total, outlet_nse, mean_sub_nse, consistency = \
        leader.build_review_prompt(
            round_id=round_id,
            worker_results=worker_results,
            current_center=vector,
            current_radius=state["radii"],
            phase=decision_phase,
            rounds_in_current_phase=state["rounds_in_current_phase"],
            diagnostics_block=diag_block if diag_block.strip() else None,
        )

    # Call LLM (stateless — single-turn, no conversation history)
    print(f"\n  Leader reviewing round {round_id}...")
    decision = None
    leader_tokens = {"prompt": 0, "completion": 0, "total": 0}
    decision_source = "llm"
    fallback_reason = None
    call_error = None
    if hasattr(leader.llm, "trace_metadata"):
        leader.llm.trace_metadata = {"role": "Leader", "round_id": round_id, "phase": phase.value, **phase_context}
    try:
        llm_resp = leader.llm.chat(
            messages=[{"role": "user", "content": review_prompt}],
            system=system_prompt,
        )
        leader_tokens = call_usage(leader, llm_resp)
        decision = llm_resp.parsed
        if decision is not None:
            validation = validate_leader_output(decision)
            if not validation.is_valid:
                logger.warning("Leader output validation failed: %s", validation.errors)
                decision = None
                fallback_reason = "leader_output_validation"
            else:
                if "round_id" not in decision:
                    decision["round_id"] = round_id
                if decision.get("stop_or_continue") not in ("continue", "stop"):
                    decision["stop_or_continue"] = "continue"
        if decision is None:
            logger.warning("Leader LLM returned non-JSON. Applying configured failure policy.")
            fallback_reason = fallback_reason or "unparsed_response"
    except Exception as e:
        decision = None
        logger.error("Leader LLM call failed (%s). Applying configured failure policy.", type(e).__name__)
        fallback_reason = type(e).__name__
        call_error = e

    if decision is None:
        recovery = record_historical_recovery(leader, fallback_reason or 'invalid_leader_response',
            round_id=round_id, phase=phase.value, error=call_error)
        leader_tokens = call_usage(leader)
        decision_source = 'historical_rule_fallback' if is_historical_fallback(leader) else 'neutral_fallback'
        decision = leader._fallback_decision(
            round_id, worker_results, j_total, outlet_nse, mean_sub_nse, phase=decision_phase,
        )
        if recovery:
            decision['_recovery'] = recovery

    # Preserve raw Leader proposals inside each guidance; normalize reason aliases and
    # make the already-unused radius explicitly inactive before downstream handoff.
    decision = normalize_decision(decision, getattr(leader, "experiment_evidence_refs", []))
    decision = bind_phase_decision(decision, phase_context)

    # Additive process evidence.
    if getattr(leader.llm, "enabled", True):
        write_round_trace(
            state["round_dir"], round_id, state.get("diagnostic_reports", []),
            state.get("coordination_result", {}), decision, worker_results,
            leader.objective_weights, phase.value,
            score_values={"J_total": j_total, "outlet_nse": outlet_nse,
                          "mean_sub_nse": mean_sub_nse, "consistency_raw": consistency,
                          "weighted_mean_nse": leader.objective_weights.get("mean_nse", .9) * mean_sub_nse,
                          "weighted_consistency_penalty": (leader.objective_weights.get("consistency", .1) * consistency
                              if leader.objective_weights.get("consistency", .1) else 0.0)},
            decision_source=decision_source, fallback_reason=fallback_reason,
            call_id=getattr(leader.llm, "last_call_id", None),
            attempt_ids=getattr(leader.llm, "last_attempt_ids", []),
            diagnostics_prompt_block=diag_block,
            diagnostic_mode=shared["coordinator"].diagnostic_mode,
        )
        write_existing_mc_diagnostics(state["round_dir"], round_id, shared["workers"],
                                      worker_results, state.get("upstream_map", {}))

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
        "leader_phase_context": phase_context,
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
    """Keep centers/radii and update measured direction evidence once per station."""
    shared = _get_shared(state)
    direction_history = copy.deepcopy(state.get("direction_history", {}))
    result_by_sid = {r["subbasin_id"]: r for r in state["round_worker_results"]}
    for sid in shared["workers"]:
        result = result_by_sid.get(sid)
        if result is None:
            continue
        direction_history = update_direction_history(
            direction_history, sid, result, round_id=state["round_id"],
            phase=state["current_phase_name"], input_fingerprint=shared.get("input_fingerprint"),
            terminal=state.get("is_final_round", False))
    return {"centers": dict(state["centers"]), "radii": dict(state["radii"]),
            "direction_history": direction_history}


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
            ok=result.get("ok") is True,
            leader_guidance=guidance,
            analysis=result.get("analysis", ""),
            phase=phase.value,
        )
        if worker.memory.history and result.get("experiment_feedback"):
            worker.memory.history[-1]["experiment_feedback"] = result["experiment_feedback"]
        if worker.memory.history and result.get("attribution_evidence"):
            # Separate from shared trial feedback: Leader never receives this E view.
            worker.memory.history[-1]["attribution_evidence"] = result["attribution_evidence"]
        worker_memories[sid] = worker.memory.to_checkpoint()

    # Write round outputs
    j_total = state["leader_j_history"][-1] if state.get("leader_j_history") else 0
    outlet_nse = state["leader_outlet_nse_history"][-1] if state.get("leader_outlet_nse_history") else 0
    mean_sub_nse = state["leader_mean_sub_nse_history"][-1] if state.get("leader_mean_sub_nse_history") else 0

    # Worker-to-worker messages
    comm_totals = dict(state.get("comm_totals", {}))
    result_by_sid = {r["subbasin_id"]: r for r in worker_results}

    for w_cfg in state["worker_cfgs"]:
        sid = w_cfg["subbasin_id"]
        for ds in w_cfg.get("neighbors_downstream", []):
            result = result_by_sid.get(sid, {})
            msg = {
                "round_id": round_id,
                "direction": "upstream_parameter_coupling",
                "natural_language_worker_exchange": False,
                "from_subbasin": sid,
                "to_subbasin": ds,
                "upstream_locked_params": (state["confirmation_vector"] if state.get("is_final_round") else state["centers"])[sid],
                "downstream_context_id": result_by_sid.get(ds, {}).get("upstream_context_id"),
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

    executed_vector = {sid: row["best_params"] for sid, row in result_by_sid.items()}
    if set(executed_vector) != {w["subbasin_id"] for w in state["worker_cfgs"]}:
        raise ExecutionIntegrityError("Joint round missing a configured station")
    execution_state = dict(state, centers=executed_vector)
    for sid, row in result_by_sid.items():
        _, expected_id = context_identity(_build_upstream_soil_info(sid, execution_state, state["worker_cfgs"]),
                                         _build_upstream_routing_info(sid, execution_state, state["worker_cfgs"]))
        if row.get("upstream_context_id") != expected_id or not is_finite(row.get("metrics", {}).get("NSE")) or row.get("ok") is not True:
            raise ExecutionIntegrityError(f"{sid}: global round does not form one finite consistent joint vector")
    receipt = {"protocol": "tra_dual_knowledge_joint_execution_v1", "round_id": round_id,
               "status": "consistent", "vector_sha256": vector_fingerprint(executed_vector),
               "params_by_subbasin": executed_vector,
               "station_execution": {sid: row["execution_identity"] for sid,row in result_by_sid.items()}}
    if state.get("is_final_round"):
        if vector_fingerprint(executed_vector) != state["confirmation_receipt"]["vector_sha256"]:
            raise ExecutionIntegrityError("Terminal vector differs from frozen highest-mean joint vector")
        receipt["confirmation_source"] = state["confirmation_receipt"]
    _write_json(round_dir / "joint_execution_receipt.json", receipt)

    # Leader decision markdown
    _write_leader_decision_md(round_dir, round_id, j_total, outlet_nse, mean_sub_nse, decision)

    # Round meta
    meta = {
        "round_id": round_id,
        "phase": phase.value,
        "leader_guidance_identity": decision.get("_guidance_identity"),
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
    if leader_tok and (leader_tok.get("total", 0) > 0 or leader_tok.get("unknown_usage_attempts", 0) > 0):
        meta["leader_tokens"] = leader_tok
    # Collect worker tokens from results
    worker_tok_list = []
    for wr in worker_results:
        wt = wr.get("llm_tokens")
        if wt and (wt.get("total", 0) > 0 or wt.get("unknown_usage_attempts", 0) > 0):
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
    """Fixed caps by default; explicit patience excludes MC and invalid rounds."""
    from .baseline_control import phase_progress
    if state.get("is_final_round"):
        return {"phase_transitioned": False}
    progress, valid = phase_progress(state)
    pipeline = state["phase_pipeline"]
    idx = state["current_phase_idx"]
    should_trans, next_idx, reason = check_phase_transition(
        pipeline, idx, state["rounds_in_current_phase"],
        progress["rounds_without_improvement_in_phase"] if valid else 0,
    )
    if not should_trans:
        return {**progress, "phase_transitioned": False}
    if next_idx is None:
        # Confirm the selected complete parameter state within the round cap.
        return {**progress, "phase_transitioned": False, "force_final_round": True}
    new_name = pipeline[next_idx]["name"]
    _require_phase_transition_centers(state)
    cache_event = _configure_phase_cache(state, new_name)
    escape_update = {}
    escape_receipt = {}
    if state.get("team_config", {}).get("method", "MADR") == "SADR":
        from .rule_worker_strategy import initial_escape_states
        escape_update = {"escape_states": initial_escape_states(state["worker_cfgs"])}
        escape_receipt = {"escape_reset": {"reason":"active_parameter_phase_changed",
            "global_ledger_preserved":True,"previous_progress":copy.deepcopy(state.get("escape_states",{})),
            "subbasin_ids":list(escape_update["escape_states"])}}
    history = list(state.get("phase_history", []))
    history.append({"from_phase": state["current_phase_name"], "to_phase": new_name,
                    "at_round": state["round_id"], "reason": reason,
                    "j_total": (state.get("leader_j_history") or [None])[-1],
                    "cache": cache_event, **escape_receipt})
    return {"current_phase_idx": next_idx, "current_phase_name": new_name,
            "rounds_in_current_phase": 0, "phase_history": history,
            "phase_transitioned": True, **escape_update,
            "direction_history": {sid: {} for sid in state.get("direction_history", {})},
            "best_mean_nse_in_phase": None, "rounds_without_improvement_in_phase": 0}



def _require_phase_transition_centers(state):
    """A phase switch may only freeze/clear cache for a measured complete accepted vector."""
    expected = {w["subbasin_id"] for w in state["worker_cfgs"]}
    centers = state.get("centers", {})
    records = state.get("accepted_center_records", {})
    if not expected or set(centers) != expected or set(records) != expected:
        raise ExecutionIntegrityError("Phase transition requires every accepted center and measured record")
    all_keys = set(get_tunable_keys(CalibrationPhase.JOINT))
    for sid in sorted(expected):
        center, record = centers[sid], records[sid]
        if (not isinstance(center, dict) or not all_keys.issubset(center)
                or not all(is_finite(center[k]) for k in all_keys)
                or not finite_success(record) or record.get("params") != center):
            raise ExecutionIntegrityError(f"{sid}: invalid or unmeasured center before phase transition")
        _, actual_context = context_identity(
            _build_upstream_soil_info(sid, state, state["worker_cfgs"]),
            _build_upstream_routing_info(sid, state, state["worker_cfgs"]))
        if record.get("upstream_context_id") != actual_context:
            raise ExecutionIntegrityError(f"{sid}: stale upstream background before phase transition")


def _configure_phase_cache(state, phase_name, executor=None):
    executor = executor or _get_shared(state)["vic_executor"]
    executor.reference_phase = phase_name
    if not state["team_config"].get("routing_cache", {}).get("enabled", True):
        return {"enabled": False}
    if phase_name == "JOINT":
        return executor.clear_routing_cache(remove_flux=True)
    if phase_name != "ROUTING_ONLY":
        return {"enabled": True, "action": "none"}
    statuses = {}
    for level in state["topo_levels"]:
        for sid in level:
            statuses[sid] = executor.prepare_routing_cache(
                sid, state["centers"][sid],
                upstream_soil_info=_build_upstream_soil_info(sid, state, state["worker_cfgs"]),
                upstream_routing_info=_build_upstream_routing_info(sid, state, state["worker_cfgs"]),
            )
    return {"enabled": True, "stations": statuses}


# ---------------------------------------------------------------------------
# 13. check_round_continuation
# ---------------------------------------------------------------------------

def check_round_continuation(state: CalibrationState) -> Dict[str, Any]:
    """Determine if calibration should continue or stop."""
    round_id = state["round_id"]
    max_rounds = state["max_rounds"]

    if round_id >= max_rounds or state.get("is_final_round", False):
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
        "representative_selection": "max_mean_nse_complete_round",
        "best_round": None,
        "best_mean_nse": None,
        "best_params_by_subbasin": {},
        "online_best_global_j_total": best,
        "j_history": state.get("leader_j_history", []),
        "outlet_nse_history": state.get("leader_outlet_nse_history", []),
        "communication_totals": state.get("comm_totals", {}),
        "end_time_utc": _utc_now(),
    }
    shared = _get_shared(state)
    from .run_contract import is_test_run
    from .process_trace import get_trace_errors
    errors = get_trace_errors(output_root)
    summary.update({"execution_accounting": shared["vic_executor"].get_accounting(),
                    "input_fingerprint": shared["input_fingerprint"],
                    "protocol_id": state["team_config"].get("protocol_id"),
                    "trace_errors": errors,
                    "audit_complete": not any(e.get("critical", True) for e in errors),
                    "test_only": is_test_run(state["team_config"], state.get("mode"), state.get("dry_run", False)),
                    "early_final_confirmation": state.get("force_final_round", False)})
    from .run_contract import audit_budget
    from .representative_result import (export_representative, select_representative_round,
                                        export_final_confirmation, RESULT_EXPORT_PROTOCOL)
    summary["result_export_protocol"] = RESULT_EXPORT_PROTOCOL
    validation_errors = []
    try:
        budget = audit_budget(output_root, state["team_config"], state["round_id"],
                              summary["execution_accounting"], state.get("is_final_round", False))
        summary["execution_budget"] = budget
        if not budget["aligned"]:
            validation_errors.append({"kind": "candidate_budget", "message": "Scheduled and recorded candidate budgets differ"})
    except Exception as error:
        validation_errors.append({"kind": "candidate_budget", "message": str(error)})
    try:
        station_ids = [w["subbasin_id"] for w in state["worker_cfgs"]]
        selected = select_representative_round(output_root, state["round_id"], station_ids)
        summary.update({"best_round": selected["selected_round_id"],
                        "best_mean_nse": selected["mean_nse"],
                        "best_params_by_subbasin": {sid: row["effective_parameters"]
                            for sid, row in selected["station_results"].items()},
                        "representative_metrics_by_subbasin": {sid: row["metrics"]
                            for sid, row in selected["station_results"].items()},
                        "representative_selection_complete": selected["selection_complete"]})
        exported = export_representative(output_root, output_root / "representative_result",
                                          state["round_id"], station_ids, online_best_global=best)
        summary["representative_export"] = exported
        summary["online_best_round_aggregate"] = {
            "round_id": selected["selected_round_id"], "mean_nse": selected["mean_nse"],
            "export_dir": "representative_result", "upstream_vector_audit": exported["upstream_vector_audit"],
        }
        if not selected["selection_complete"]:
            validation_errors.append({"kind": "representative_selection", "message": "Missing or invalid main rounds; selection is provisional"})
    except Exception as error:
        validation_errors.append({"kind": "representative_export", "message": str(error)})
    try:
        confirmation = export_final_confirmation(output_root, output_root / "final_confirmation_result",
                                                   state["round_id"], station_ids, online_best_global=best)
        summary["final_confirmation_export"] = confirmation
        summary["final_confirmation_round"] = confirmation["selected_round_id"]
        summary["final_confirmation_mean_nse"] = confirmation["mean_nse"]
    except Exception as error:
        validation_errors.append({"kind": "final_confirmation_export", "message": str(error)})
    try:
        summary['llm_usage_accounting'] = summarize_attempt_usage(output_root)
    except Exception as error:
        validation_errors.append({'kind': 'llm_usage_accounting', 'message': type(error).__name__})
    try:
        summary["llm_recovery_accounting"] = summarize_recovery(output_root)
    except Exception as error:
        validation_errors.append({"kind": "llm_recovery_accounting", "message": type(error).__name__})
    summary["protocol_validation_errors"] = validation_errors
    summary["audit_complete"] = summary["audit_complete"] and not validation_errors
    _write_json(output_root / "final_summary.json", summary)

    print(f"\nCalibration complete. Results: {output_root}")
    print(f"Representative round: {summary['best_round']}, mean NSE: {summary['best_mean_nse']}")
    print(f"Online best J_total (separate): round {best.get('round_id', -1)}, {best.get('score', 0):.5f}")

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
