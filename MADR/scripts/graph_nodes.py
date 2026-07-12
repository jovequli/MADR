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
import math
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
from .executors import make_executor, ModelExecutor
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


def _period_date(period: Dict, which: str, label: str) -> Optional[str]:
    """Return the ISO `<which>_date` from a period dict, or None.

    `which` is "start" or "end". Invalid dates log a loud warning and
    return None (never a silent fallback).
    """
    raw = period.get(f"{which}_date")
    if raw is None:
        return None
    try:
        return dt.date.fromisoformat(str(raw)).isoformat()
    except ValueError:
        logger.warning("%s.%s_date=%r is not an ISO date (YYYY-MM-DD); ignoring it.",
                       label, which, raw)
        return None


def _period_year(period: Dict, which: str, label: str) -> Optional[int]:
    """Resolve the start/end year of a period dict (`which` is "start"/"end").

    Accepts `<which>_year` (int) or `<which>_date` (ISO string; the year is
    derived). Mid-year dates log a warning: VIC/CaMa evaluate whole calendar
    years, so only the date's year is used (SWAT consumes the full date).
    Unparseable values log loudly and return None for an explicit fallback
    at the call site.
    """
    year_key = f"{which}_year"
    if year_key in period:
        try:
            return int(period[year_key])
        except (TypeError, ValueError):
            logger.warning("%s.%s=%r is not an integer year; trying %s_date.",
                           label, year_key, period[year_key], which)
    iso = _period_date(period, which, label)
    if iso is None:
        return None
    d = dt.date.fromisoformat(iso)
    whole_year = (d.month, d.day) == ((1, 1) if which == "start" else (12, 31))
    if not whole_year:
        logger.warning(
            "%s.%s_date=%s is mid-year; VIC/CaMa evaluate on whole years, "
            "using year %d (SWAT keeps the full date).",
            label, which, iso, d.year)
    return d.year


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
    model_combination = state.get("model_combination", "vic_routing")
    # Outlet comes from the config; when absent, derive it from the worker
    # flagged is_outlet (never assume any specific basin). An empty outlet_id
    # collapses to the generic sentence inside the agents' prompts.
    outlet_id = (cfg.get("outlet_subbasin_id")
                 or next((w["subbasin_id"] for w in worker_cfgs
                          if w.get("is_outlet")), ""))
    topology_note = _topology_note(worker_cfgs, outlet_id)
    leader = LeaderAgent(
        agent_id=leader_cfg["agent_id"],
        llm_client=leader_llm,
        workers=worker_cfgs,
        objective_weights=state.get("objective_weights", {}),
        stop_rules=state.get("stop_rules", {}),
        param_ranges=range_lists,
        model_combination=model_combination,
        topology_note=topology_note,
        n_basins=len(worker_cfgs),
        outlet_id=outlet_id,
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
            model_combination=model_combination,
            topology_note=topology_note,
            n_basins=len(worker_cfgs),
            outlet_id=outlet_id,
        )
        workers[sid].set_output_dir(str(output_root))
        # Propagate the combination to the code-fallback executor so its
        # phase-key table matches (WorkerExecutor defaults to 'vic_routing').
        workers[sid].executor.model_combination = model_combination

    # Model-combination executor (VIC+Routing, VIC+CaMa-Flood, ...)
    # `vic_executor` is kept as the variable name for backward compat with the
    # many call sites below; the object is now a ModelExecutor whose `.run()`
    # / `.register_basin()` / `.validate_all()` surface is the same.
    vic_executor: ModelExecutor = make_executor(model_combination, mode=mode)
    if mode == "real":
        top_cama_cfg = cfg.get("cama_flood_config", {})
        sim_period = cfg.get("simulation_period", {})
        spin_up_years = int(sim_period.get("spin_up_years", 0))
        # Lenient simulation_period parsing: year keys preferred, ISO date
        # keys accepted (years derived). Used for eval-window clamping and
        # for the cama simulate window below.
        sim_start_year = _period_year(sim_period, "start", "simulation_period") if sim_period else None
        sim_end_year = _period_year(sim_period, "end", "simulation_period") if sim_period else None
        # Optional explicit evaluation window: simulation runs exactly
        # simulation_period.start_year..end_year, metrics are computed only
        # on evaluation_period, and spin_up_years is ignored.
        eval_period = cfg.get("evaluation_period") or {}
        eval_start_year: Optional[int] = None
        eval_end_year: Optional[int] = None
        eval_start_date: Optional[str] = None
        eval_end_date: Optional[str] = None
        if eval_period:
            sim_start = 1979 if sim_start_year is None else sim_start_year
            sim_end = 1980 if sim_end_year is None else sim_end_year
            # evaluation_period accepts start_year/end_year ints OR
            # start_date/end_date ISO strings.
            eval_start_year = _period_year(eval_period, "start", "evaluation_period")
            eval_end_year = _period_year(eval_period, "end", "evaluation_period")
            if eval_start_year is None:
                logger.warning(
                    "evaluation_period start missing/unparseable (%r); "
                    "falling back to simulation start year %s.",
                    eval_period, sim_start)
                eval_start_year = sim_start
            if eval_end_year is None:
                logger.warning(
                    "evaluation_period end missing/unparseable (%r); "
                    "falling back to simulation end year %s.",
                    eval_period, sim_end)
                eval_end_year = sim_end
            if eval_start_year < sim_start or eval_end_year > sim_end:
                logger.warning(
                    "evaluation_period %s-%s outside simulation_period %s-%s; clamping.",
                    eval_start_year, eval_end_year, sim_start, sim_end,
                )
                eval_start_year = max(eval_start_year, sim_start)
                eval_end_year = min(eval_end_year, sim_end)
            # Full-date evaluation window (SWAT consumes dates natively);
            # whole-year dates are derived when only year keys are given.
            eval_start_date = (_period_date(eval_period, "start", "evaluation_period")
                               or f"{eval_start_year}-01-01")
            eval_end_date = (_period_date(eval_period, "end", "evaluation_period")
                             or f"{eval_end_year}-12-31")
            if spin_up_years:
                logger.warning(
                    "Both evaluation_period and spin_up_years=%s are set; "
                    "spin_up_years is ignored.", spin_up_years,
                )
        # Universal spin_up_years (swat / vic_cama_flood): vic_routing plumbs
        # vic_cfg.spin_up_years natively (vic_runner extends the simulation
        # backward), but SWATExecutor and VICCamaFloodExecutor don't. When
        # spin_up_years > 0 and no evaluation_period is given, shift the
        # simulated window back N years and evaluate from the ORIGINAL start
        # (swatplus_runner extends the working copy's time.sim; the CaMa
        # executor rewrites the VIC template STARTYEAR — both gated on the
        # markers set below, so legacy configs stay byte-identical).
        # With evaluation_period present, the round-2 rule above stands
        # (spin_up_years ignored with a warning).
        spin_up_shift = 0
        if (spin_up_years > 0 and not eval_period
                and model_combination in ("swat", "vic_cama_flood")):
            spin_up_shift = spin_up_years
        for w_cfg in worker_cfgs:
            sid = w_cfg["subbasin_id"]

            if model_combination == "swat":
                # SWAT+ basin config has its own field layout (TxtInOut template,
                # HRU ids, channel id, simulation dates). Build a SWATPlusConfig
                # and register; SWATExecutor.register_basin coerces dict->config.
                from .swatplus_executor import SWATPlusConfig
                start_date = sim_period.get("start_date") or f"{sim_period.get('start_year', 1980)}-01-01"
                end_date = sim_period.get("end_date") or f"{sim_period.get('end_year', 1990)}-12-31"
                swat_eval_start, swat_eval_end = eval_start_date, eval_end_date
                if spin_up_shift:
                    # Warm-up: simulate from N years earlier, score metrics
                    # from the original start. swatplus_runner extends each
                    # working copy's time.sim backward to the shifted start
                    # (gated on the spin_up_years attribute attached below;
                    # templates that already start early enough stay as-is).
                    swat_eval_start, swat_eval_end = start_date, end_date
                    d = dt.date.fromisoformat(start_date)
                    try:
                        d = d.replace(year=d.year - spin_up_shift)
                    except ValueError:  # Feb 29 -> Feb 28 in non-leap years
                        d = d.replace(year=d.year - spin_up_shift, day=28)
                    start_date = d.isoformat()
                    logger.info(
                        "spin_up_years=%d (swat, %s): simulation start "
                        "shifted to %s (time.sim extended per run), metrics "
                        "evaluated on %s..%s.",
                        spin_up_shift, sid, start_date, swat_eval_start,
                        swat_eval_end)
                scratch_root = cfg.get("swatplus_scratch_dir") or str(output_root)
                swat_cfg = SWATPlusConfig(
                    basin_name=sid,
                    base_output_dir=str(scratch_root),
                    txtinout_template=w_cfg.get("txtinout_template", cfg.get("txtinout_template", "")),
                    swatplus_exe=w_cfg.get("swatplus_exe", cfg.get("swatplus_exe", "")),
                    obs_file=w_cfg.get("obs_file", ""),
                    hru_fids=list(w_cfg.get("hru_fids", [])),
                    subbasin_attrs_file=w_cfg.get(
                        "subbasin_attrs_file",
                        cfg.get("subbasin_attrs_file", ""),
                    ),
                    channel_id=int(w_cfg.get("channel_id", 0)),
                    start_date=start_date,
                    end_date=end_date,
                )
                if swat_eval_start or swat_eval_end:
                    # Evaluation window: SWATPlusConfig has no eval fields
                    # yet, so attach them dynamically; swatplus_runner reads
                    # them via getattr with an empty default, keeping legacy
                    # behavior identical when evaluation_period is absent.
                    swat_cfg.eval_start_date = swat_eval_start
                    swat_cfg.eval_end_date = swat_eval_end
                if spin_up_shift:
                    # Ask swatplus_runner to extend the working time.sim
                    # backward to start_date's year. Attached dynamically
                    # (same pattern as the eval fields) so configs without
                    # spin_up_years keep the template's time.sim verbatim.
                    swat_cfg.spin_up_years = spin_up_shift
                vic_executor.register_basin(sid, swat_cfg)
                continue

            # VIC-based combos build a VICConfig from the per-worker fields.
            vic_cfg = VICConfig(
                basin_name=sid,
                base_output_dir=str(output_root),
                soil_base=w_cfg.get("soil_base", ""),
                global_base=w_cfg.get("global_base", ""),
                obs_file=w_cfg.get("obs_file", ""),
                rout_template_dir=w_cfg.get("rout_template_dir", ""),
                vic_exe=w_cfg.get("vic_exe", cfg.get("vic_exe", "")),
                rout_exe=w_cfg.get("rout_exe", cfg.get("rout_exe", "")),
                # Date-only simulation_period configs (lenient parsing above)
                # must not fall back to the 1979/1980 defaults here: the CaMa
                # executor compares start_year against the template STARTYEAR
                # for the warm-up rewrite. Year keys, when present, win as
                # before.
                start_year=sim_period.get(
                    "start_year", 1979 if sim_start_year is None else sim_start_year),
                end_year=sim_period.get(
                    "end_year", 1980 if sim_end_year is None else sim_end_year),
                # Legacy default kept so old configs that omit flux_prefix
                # keep running; new basins should set flux_prefix per worker.
                flux_prefix=w_cfg.get("flux_prefix", "huaihe_fluxes_"),
                vic_flux_source=w_cfg.get("vic_flux_source", ""),
                spin_up_years=spin_up_years,
                eval_start_year=eval_start_year,
                eval_end_year=eval_end_year,
            )
            if model_combination == "vic_cama_flood":
                # Per-worker CaMa config overrides shared top-level defaults so a
                # multi-station run gives each basin its own map/diminfo/outlet/obs.
                w_cama = {**top_cama_cfg, **w_cfg.get("cama_flood_config", {})}
                cama_start_year, cama_eval_start = sim_start_year, eval_start_year
                if spin_up_shift:
                    if sim_start_year is None:
                        logger.warning(
                            "spin_up_years=%d is not supported for "
                            "vic_cama_flood without a parseable "
                            "simulation_period start and was IGNORED for %s.",
                            spin_up_shift, sid)
                    else:
                        # Warm-up via the round-3 CaMa eval window:
                        # VICCamaFloodExecutor never consumes
                        # vic_cfg.spin_up_years (it builds its own
                        # RuntimeConfig; see _run_vic_fluxes), so extend both
                        # the VIC flux run and the CaMa window backward by N
                        # years and evaluate from the ORIGINAL start_year.
                        cama_start_year = sim_start_year - spin_up_shift
                        cama_eval_start = sim_start_year
                        vic_cfg.start_year = int(vic_cfg.start_year) - spin_up_shift
                        vic_cfg.spin_up_years = 0  # shift replaces the unplumbed native path
                        # Marker consumed by VICCamaFloodExecutor: only a
                        # spin-up-shifted start may rewrite the template
                        # STARTYEAR (legacy configs never did).
                        vic_cfg.sim_start_extended = True
                        logger.info(
                            "spin_up_years=%d (vic_cama_flood, %s): simulate "
                            "%d..%s, metrics evaluated from %d.",
                            spin_up_shift, sid, cama_start_year, sim_end_year,
                            cama_eval_start)
                # Top-level periods win over cama-specific years (which
                # remain as fallback when the top-level key is absent):
                # simulate simulation_period, evaluate evaluation_period.
                for key, top_val in (("start_year", cama_start_year),
                                     ("end_year", sim_end_year),
                                     ("eval_start_year", cama_eval_start),
                                     ("eval_end_year", eval_end_year)):
                    if top_val is None:
                        continue
                    cama_val = w_cama.get(key)
                    if cama_val is not None and int(cama_val) != int(top_val):
                        logger.warning(
                            "cama_flood_config.%s=%s disagrees with top-level "
                            "period value %s for %s; top-level wins.",
                            key, cama_val, top_val, sid)
                    w_cama[key] = int(top_val)
                vic_executor.register_basin(sid, {"vic": vic_cfg, "cama_flood": w_cama})
            else:
                vic_executor.register_basin(sid, vic_cfg)

        # Validate all model paths before any execution.
        issues = vic_executor.validate_all()
        if issues:
            for basin, missing in issues.items():
                logger.error("Model config [%s] missing paths: %s", basin, missing)
            raise RuntimeError(
                f"Model path validation failed for {len(issues)} basin(s): "
                + ", ".join(f"{b}({len(m)} missing)" for b, m in issues.items())
            )

    # Leader coordinator
    upstream_map = state["upstream_map"]
    coordinator = LeaderCoordinator(
        upstream_map=upstream_map,
        outlet_id=outlet_id,
        model_combination=model_combination,
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

def _topology_note(worker_cfgs: List[Dict], outlet_id: str) -> str:
    """Compact human-readable topology line for prompt placeholders.

    Example: "A -> C; B -> C; C = outlet (3 basins)".
    """
    parts: List[str] = []
    for w in worker_cfgs:
        sid = w["subbasin_id"]
        downstream = w.get("neighbors_downstream", []) or []
        if downstream:
            parts.append(f"{sid} -> {', '.join(downstream)}")
        elif w.get("is_outlet", False) or sid == outlet_id:
            parts.append(f"{sid} = outlet")
        else:
            parts.append(f"{sid} (no downstream)")
    return "; ".join(parts) + f" ({len(worker_cfgs)} basins)"


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


def _validate_topology(worker_cfgs: List[Dict], outlet_id: str) -> None:
    """Fail fast on an invalid basin topology (before any graph work).

    Raises ValueError on (1) a neighbors_downstream id that is not a
    configured subbasin_id (typo guard — _build_upstream_map would silently
    drop the edge), (2) a cycle (the topological ordering cannot consume
    every node), (3) an outlet_subbasin_id that is not a configured subbasin.
    """
    valid_ids = [w["subbasin_id"] for w in worker_cfgs]
    valid_set = set(valid_ids)
    for w in worker_cfgs:
        for ds in w.get("neighbors_downstream", []) or []:
            if ds not in valid_set:
                raise ValueError(
                    f"neighbors_downstream id {ds!r} declared by worker "
                    f"{w['subbasin_id']!r} is not a configured subbasin_id "
                    f"(valid ids: {valid_ids})")
    if outlet_id and outlet_id not in valid_set:
        raise ValueError(
            f"outlet_subbasin_id {outlet_id!r} is not a configured "
            f"subbasin_id (valid ids: {valid_ids})")
    consumed = {sid
                for level in _compute_topo_levels(_build_upstream_map(worker_cfgs))
                for sid in level}
    cycle_nodes = sorted(valid_set - consumed)
    if cycle_nodes:
        raise ValueError(
            "neighbors_downstream topology contains a cycle involving "
            f"{cycle_nodes}: topological ordering cannot consume these nodes")


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


def _build_upstream_cama_info(
    subbasin_id: str, state: CalibrationState, worker_cfgs: List[Dict],
) -> List[Dict[str, Any]]:
    """Per-reach CaMa channel-param lock (vic_cama_flood nested basins).

    For each upstream basin, return its locked channel params (Hc/Hmin/Wc/Wmin
    from the current centers — fixed after the upstream batch) plus that basin's
    cama_flood_config (so the executor can find its map and splice the upstream
    reach's channel geometry into the downstream grid). Mirrors
    _build_upstream_soil_info; returns [] for headwaters or vic_routing.
    """
    upstream_map = state["upstream_map"]
    full_upstream_map = state.get("full_upstream_map", {})
    centers = state["centers"]

    all_upstream = _collect_all_upstream(subbasin_id, upstream_map, full_upstream_map)
    if not all_upstream:
        return []

    worker_cfg_by_sid = {w["subbasin_id"]: w for w in worker_cfgs}
    infos: List[Dict[str, Any]] = []
    for up_sid in all_upstream:
        up_cfg = worker_cfg_by_sid.get(up_sid)
        if not up_cfg:
            continue
        cama_cfg = up_cfg.get("cama_flood_config")
        if not cama_cfg:
            continue
        up_center = centers.get(up_sid, {})
        params = {k: up_center[k] for k in ("Hc", "Hmin", "Wc", "Wmin")
                  if k in up_center}
        if params:
            infos.append({"subbasin_id": up_sid,
                          "params": params,
                          "cama_flood_config": cama_cfg})
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

    # Topology (validated fail-fast: a typo'd downstream id, a cycle, or a
    # bad outlet_subbasin_id must abort here, not misbehave mid-round).
    _validate_topology(worker_cfgs, str(cfg.get("outlet_subbasin_id") or ""))
    upstream_map = _build_upstream_map(worker_cfgs)
    topo_levels = _compute_topo_levels(upstream_map)
    # SWAT+ executes the whole watershed as ONE joint simulation per round
    # (see _execute_batch_workers_swat), so upstream-first level sequencing
    # is meaningless: collapse all topo levels into a single batch.
    if cfg.get("model_combination", "vic_routing") == "swat" and len(topo_levels) > 1:
        topo_levels = [sorted(upstream_map.keys())]

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

    # Phase pipeline.  The configured/CLI cap counts optimization rounds.
    # One additional slot is always reserved for the mandatory force-best
    # verification run, so a full pipeline never loses its last search round.
    phase_pipeline = load_phase_pipeline(cfg)
    cli_max_rounds = int(state.get("max_rounds", 0) or 0)
    config_max_rounds = int(cfg.get("max_rounds", 0) or 0)
    optimization_max_rounds = (
        cli_max_rounds
        or config_max_rounds
        or get_total_max_rounds(phase_pipeline)
    )
    max_rounds = optimization_max_rounds + 1

    # Objective weights and stop rules
    objective_weights = cfg.get("objective_weights", {"mean_nse": 0.90, "consistency": 0.10})
    stop_rules = cfg.get("stop_rules", {
        "outlet_nse_target": 0.85, "min_improvement": 0.003, "patience_rounds": 3,
    })

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
        "optimization_max_rounds": optimization_max_rounds,
        "max_rounds": max_rounds,
        "final_round_is_additional": True,
        "objective_weights": objective_weights,
        "stop_rules": stop_rules,
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
          f"{len(phase_pipeline)} phases, {optimization_max_rounds} optimization "
          f"rounds + 1 final verification round")

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
        "stop_rules": stop_rules,
        "phase_pipeline": phase_pipeline,
        "phase_nse_thresholds": phase_nse_thresholds,
        "optimization_max_rounds": optimization_max_rounds,
        "max_rounds": max_rounds,
        "max_parallel_workers": int(cfg.get("max_parallel_workers", 4)),
        "centers": centers,
        "radii": radii,
        "direction_history": direction_history,
        "swat_best_bundle": None,
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
        # Which model combination's prompts + executor this run uses.
        # Default "vic_routing" preserves pre-refactor behaviour.
        "model_combination": cfg.get("model_combination", "vic_routing"),
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
        n_random=int(explore_cfg.get("n_random", 40)),
    )
    summary = format_exploration_summary(results)

    # Save summary
    explore_out = {
        sid: {"best_nse": r["best_nse"], "best_params": r["best_params"],
              "n_samples": r["n_samples"], "summary": r["summary_text"]}
        for sid, r in results.items()
    }
    _write_json(output_root / "initial_exploration.json", explore_out)

    # Save full per-basin exploration results.
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
      - the extra final slot after the optimization-round cap is reached.
    """
    round_id = state.get("round_id", 0) + 1
    max_rounds = state["max_rounds"]  # includes one extra final slot
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

    # SWAT+ executes jointly: all workers' params go into ONE batched
    # simulation per strategy (proposals stay per-worker). Non-swat
    # combinations keep the per-worker path below unchanged.
    if state.get("model_combination", "vic_routing") == "swat":
        if hasattr(_get_shared(state)["vic_executor"], "run_batched"):
            return _execute_batch_workers_swat(state, sids)
        logger.warning(
            "model_combination='swat' but executor %s lacks the run_batched "
            "interface; falling back to legacy per-worker execution.",
            type(_get_shared(state)["vic_executor"]).__name__,
        )

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

        # Inject per-basin exploration summary for Worker prompt (matches original TRA)
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

        # Upstream info (cama info only for vic_cama_flood; other executors'
        # run() does not accept the upstream_cama_info kwarg)
        up_routing_info = _build_upstream_routing_info(sid, state, worker_cfgs)
        up_soil_info = _build_upstream_soil_info(sid, state, worker_cfgs)
        up_cama_info = None
        if state.get("model_combination", "vic_routing") == "vic_cama_flood":
            up_cama_info = _build_upstream_cama_info(sid, state, worker_cfgs)

        # R1 (no leader decision yet) → exact defaults, skip MC even if enabled
        if use_multi and prev_decision is not None:
            result, mc_line = _run_worker_multi(
                worker, vic_executor, round_id, center, radius,
                guidance, round_dir, state.get("mc_n_candidates", 5),
                phase, up_routing_info, up_soil_info, up_cama_info,
            )
            return result, mc_line
        else:
            result = _run_worker_single(
                worker, vic_executor, round_id, center, radius,
                guidance, round_dir, phase, up_routing_info, up_soil_info,
                up_cama_info, force_best=force_best,
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
    round_dir, phase, up_routing, up_soil, up_cama=None, force_best=False,
) -> Dict:
    """Run a single-candidate worker round. Mirrors run_worker_round()."""
    sid = worker.subbasin_id
    t0 = dt.datetime.now(dt.timezone.utc)
    locked_keys = set(get_locked_keys(phase, model_combination=worker.model_combination))
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
    elif (guidance and guidance.get("param_adjustments")) or round_id > 1:
        if guidance and guidance.get("param_adjustments"):
            priority = guidance.get("priority_focus", "")
            adj_hints = guidance.get("param_adjustments", [])
            hint_strs = [
                f"{a.get('param', '?')}: {a.get('direction', '?')} ({a.get('magnitude', '')})"
                for a in adj_hints[:4]
            ]
            leader_context = f"{priority}. Hints: {', '.join(hint_strs)}" if hint_strs else priority
        else:
            # No leader hints this round: still ask the Worker LLM so the
            # round isn't wasted re-running the identical center params.
            priority = guidance.get("priority_focus", "") if guidance else ""
            no_hint_note = ("No specific leader guidance this round; propose your "
                            "own adjustment based on your diagnosis history.")
            leader_context = f"{priority}. {no_hint_note}" if priority else no_hint_note

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
    cama_kwargs = {"upstream_cama_info": up_cama} if up_cama is not None else {}
    ok, metrics = vic_executor.run(
        subbasin_id=sid,
        run_id=round_id * 100 + 1,
        candidate_id=1,
        params=params,
        phase_tag=f"round_{round_id}_c1",
        upstream_routing_info=up_routing,
        upstream_soil_info=up_soil,
        **cama_kwargs,
    )
    score = _score_row(metrics) if ok else float("-inf")
    duration = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()

    # Save routing result (executor knows its own output layout)
    if ok:
        vic_executor.copy_routing_artifacts(sid, round_dir / "routing_result" / sid)

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
    round_dir, n_candidates, phase, up_routing, up_soil, up_cama=None,
) -> Tuple[Dict, str]:
    """Run an LLM-based multi-candidate Worker round.

    1. Call Worker LLM n_candidates times (conservative / moderate / bold).
    2. Each call produces a full parameter set directly.
    3. Run all candidates through VIC and pick the best.
    4. Record which strategy won — fed back in next round's history.
    """
    MC_STRATEGIES = ["conservative", "moderate", "bold", "exploratory", "reverse"]

    sid = worker.subbasin_id
    t0 = dt.datetime.now(dt.timezone.utc)
    locked_keys = set(get_locked_keys(phase, model_combination=worker.model_combination))
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
    cama_kwargs = {"upstream_cama_info": up_cama} if up_cama is not None else {}
    eval_results = []
    for i, (params, label, reasoning) in enumerate(candidates):
        ok, metrics = vic_executor.run(
            subbasin_id=sid,
            run_id=round_id * 100 + i + 1,
            candidate_id=i + 1,
            params=params,
            phase_tag=f"round_{round_id}_mc{i+1}_{label}",
            upstream_routing_info=up_routing,
            upstream_soil_info=up_soil,
            **cama_kwargs,
        )
        # Save this candidate's routing result before next run overwrites it
        if ok:
            vic_executor.copy_routing_artifacts(
                sid,
                round_dir / "routing_result" / sid,
                candidate_tag=f"c{i+1}_{label}",
            )
        nse = metrics.get("NSE", float("-inf")) if ok else float("-inf")
        score = _score_row(metrics) if ok else float("-inf")
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


def _swat_pick_joint_strategy(
    per_strategy: List[Dict[str, Tuple[bool, Dict[str, float]]]],
    sids: List[str],
    sel_metric: str,
    leader_score,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Score each joint SWAT strategy and pick the basin-wide best.

    sel_metric "mean_nse" (default) reproduces the legacy selection
    byte-for-byte; "j_total" scores each strategy with the leader's exact
    objective — `leader_score` is LeaderAgent.compute_global_score (the
    reference implementation in leader_agent.py: J = w_mean_nse·mean(NSE)
    − w_consistency·mean(|peak_ratio−1|, |lowflow_ratio−1|), NaN-safe),
    passed in so weights and NaN-skipping stay single-sourced. Eligibility
    (joint run ok + finite NSE on every site) is metric-independent.
    Returns (strategy_stats, best_stat).
    """
    strategy_stats = []
    for k in range(len(per_strategy)):
        nses = []
        for sid in sids:
            ok, metrics = per_strategy[k][sid]
            k_nse = metrics.get("NSE", float("nan"))
            if ok and math.isfinite(k_nse):
                nses.append(k_nse)
        stat = {
            "idx": k,
            "eligible": len(nses) == len(sids),
            "n_valid": len(nses),
            "mean_nse": (sum(nses) / len(nses)) if nses else float("-inf"),
        }
        if sel_metric == "j_total":
            stat["score"] = leader_score(
                [{"subbasin_id": sid, "metrics": per_strategy[k][sid][1]}
                 for sid in sids])[0]
        else:
            stat["score"] = stat["mean_nse"]
        strategy_stats.append(stat)
    eligible_stats = [s for s in strategy_stats if s["eligible"]]
    if eligible_stats:
        best_stat = max(eligible_stats, key=lambda s: s["score"])
    else:
        # No strategy succeeded on every site: keep the most complete run
        # (tie-break by partial score) and let failed sites' NaN metrics
        # flow through so the round is marked invalid downstream.
        best_stat = max(strategy_stats, key=lambda s: (s["n_valid"], s["score"]))
    return strategy_stats, best_stat


def _swat_update_best_bundle(
    state: CalibrationState,
    batch_results: List[Dict[str, Any]],
    centers: Dict[str, Dict[str, float]],
    leader_score,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Watershed-level joint best bundle acceptance (SWAT rounds only).

    Builds this round's candidate bundle from the adopted joint strategy
    (all workers share one k*; see _execute_batch_workers_swat) and accepts
    or rejects it AS A WHOLE against the stored state["swat_best_bundle"],
    compared on the metric named by team_config.mc_selection_metric — the
    SAME key that governs intra-round k* selection, so the two criteria can
    never diverge. j_total comes from LeaderAgent.compute_global_score
    exactly as _swat_pick_joint_strategy uses it; both j_total and mean_nse
    are always recorded. A candidate with any non-finite station NSE, or a
    non-finite selection score, never wins. Accepted → EVERY center adopts
    the bundle's params; rejected → EVERY center is re-pinned to the
    previous bundle (mixed-round centers are impossible). With no stored
    bundle (fresh run or a resumed pre-bundle checkpoint), the first
    completed round with a finite score seeds the bundle.

    Mutates `centers` in place; returns (bundle, accepted).
    """
    sids = [r["subbasin_id"] for r in batch_results]
    params_by_sid = {}
    metrics_by_sid = {}
    for r in batch_results:
        sid = r["subbasin_id"]
        params_by_sid[sid] = dict(r.get("best_params") or r.get("params") or {})
        metrics_by_sid[sid] = dict(r.get("metrics") or {})
    nses = []
    for sid in sids:
        try:
            nse = float(metrics_by_sid[sid].get("NSE", float("nan")))
        except (TypeError, ValueError):
            nse = float("nan")
        if math.isfinite(nse):
            nses.append(nse)
    eligible = len(nses) == len(sids)
    j_total = leader_score(
        [{"subbasin_id": sid, "metrics": metrics_by_sid[sid]} for sid in sids])[0]
    candidate = {
        "round_id": state["round_id"],
        # Single-candidate rounds carry no global_strategy_index; the one
        # (and only) joint run is strategy 1 by construction.
        "strategy_index": batch_results[0].get("global_strategy_index", 1),
        "params_by_sid": params_by_sid,
        "metrics_by_sid": metrics_by_sid,
        "mean_nse": (sum(nses) / len(nses)) if nses else float("-inf"),
        "j_total": j_total,
    }

    sel_metric = str(state["team_config"].get("mc_selection_metric", "mean_nse"))
    if sel_metric not in ("mean_nse", "j_total"):
        sel_metric = "mean_nse"
    try:
        cand_score = float(candidate[sel_metric])
    except (TypeError, ValueError):
        cand_score = float("nan")
    cand_ok = eligible and math.isfinite(cand_score)

    prev = state.get("swat_best_bundle")
    if prev is None:
        accepted = cand_ok
    else:
        try:
            prev_score = float(prev.get(sel_metric, float("-inf")))
        except (TypeError, ValueError):
            prev_score = float("-inf")
        if not math.isfinite(prev_score):
            prev_score = float("-inf")
        accepted = cand_ok and cand_score > prev_score

    bundle = candidate if accepted else prev
    # Centers follow the bundle as a whole: the accepted round's params for
    # every worker, or the previous bundle's params for every worker.
    if bundle is not None:
        param_ranges = state["param_ranges"]
        for sid, params in bundle.get("params_by_sid", {}).items():
            if sid in centers and params:
                centers[sid] = normalize_params(dict(params), param_ranges)
    return bundle, accepted


def _build_probe_specs(
    probe_names: List[str],
    param_ranges: Dict[str, Any],
    centers: Dict[str, Dict[str, float]],
    sids: List[str],
    phase_radius: Dict[str, Any],
    top_radius: Dict[str, Any],
    locked_keys: set,
) -> List[Dict[str, Any]]:
    """Build deterministic single-parameter probe strategies (probe_params).

    Strategy k perturbs EXACTLY ONE listed parameter away from each worker's
    current center. Delta is the phase-level ``initial_radius`` value for
    that param if present, else the top-level ``initial_radius`` value, else
    15% of the range span. Direction is upward unless any worker's center
    sits within one delta of the upper bound (the probe would just clamp
    there), in which case downward. Params with no range, a frozen range
    (lo == hi), or a phase lock are skipped with a warning.

    Returns [{"param", "delta", "sign", "label"}, ...] in listed order.
    """
    specs: List[Dict[str, Any]] = []
    for name in probe_names:
        bounds = param_ranges.get(name)
        if bounds is None:
            logger.warning("probe_params: %s has no range; skipped.", name)
            continue
        lo, hi = float(bounds[0]), float(bounds[1])
        if lo == hi:
            logger.warning(
                "probe_params: %s is frozen (lo==hi==%g); skipped.", name, lo)
            continue
        if name in locked_keys:
            logger.warning(
                "probe_params: %s is locked in this phase; skipped.", name)
            continue
        delta = phase_radius.get(name, top_radius.get(name))
        delta = abs(float(delta)) if delta is not None else 0.15 * (hi - lo)
        sign = 1.0
        if any(float(centers[sid].get(name, (lo + hi) / 2)) + delta > hi
               for sid in sids):
            sign = -1.0
        label = f"probe:{name}{'+' if sign > 0 else '-'}{delta:g}"
        specs.append(
            {"param": name, "delta": delta, "sign": sign, "label": label})
    return specs


def _execute_batch_workers_swat(state: CalibrationState, sids: List[str]) -> Dict[str, Any]:
    """SWAT+ joint execution path.

    Worker PROPOSALS stay per-worker (parallel LLM calls, leader guidance,
    anti-flip, phase locks — same as the legacy path). EXECUTION is joint:
    one batched SWAT+ simulation per strategy index applies every worker's
    k-th candidate on mutually exclusive HRU sets (executor.run_batched),
    so metrics come from ONE unified parameter field. A single strategy
    index k* is then chosen basin-wide (best mean NSE across all sites by
    default; team_config.mc_selection_metric="j_total" selects by the
    leader's J objective instead — see _swat_pick_joint_strategy),
    and EVERY worker adopts its own params and metrics from that same
    joint run — mixing k's across workers would build a parameter field
    that was never simulated. The final force-best round runs one
    batched simulation with every worker's historical best params.

    Probe mode (optional): when the current phase's raw
    ``phase_config.pipeline[i]`` entry carries ``probe_params``, MC rounds
    replace worker LLM proposals with deterministic single-parameter probes
    of the current centers (see _build_probe_specs) for empirical direction
    verification; execution, global-k* selection, and center adoption are
    unchanged, and a per-probe dNSE summary (vs each site's center baseline:
    the accepted joint bundle's NSE when a bundle exists, else the ledger
    best) is appended to mc_round_summaries plus a probe_responses JSON in
    the round's results dir. probe_params applies to this SWAT joint path
    ONLY; the vic_routing per-worker MC path does not read it.
    """
    MC_STRATEGIES = ["conservative", "moderate", "bold", "exploratory", "reverse"]

    round_id = state["round_id"]
    shared = _get_shared(state)
    vic_executor = shared["vic_executor"]
    phase = CalibrationPhase(state["current_phase_name"])
    phase_str = phase.value
    force_best = state.get("is_final_round", False)
    prev_decision = state.get("leader_decision")
    use_multi = (state.get("use_multi_candidate", False) and not force_best
                 and prev_decision is not None)
    n_candidates = state.get("mc_n_candidates", 5)
    max_parallel = state.get("max_parallel_workers", 4)
    round_dir = Path(state["round_dir"])
    locked_keys = set(get_locked_keys(phase, model_combination="swat"))
    t0 = dt.datetime.now(dt.timezone.utc)

    # ── Optional probe mode: deterministic single-parameter strategies ──
    # Only on MC rounds of a phase whose raw pipeline entry lists
    # probe_params; with the key absent, probe_specs stays empty and every
    # branch below is byte-identical to the legacy behavior.
    probe_specs: List[Dict[str, Any]] = []
    if use_multi:
        raw_pipeline = state["team_config"].get("phase_config", {}).get("pipeline", [])
        phase_idx = state.get("current_phase_idx", 0)
        if (0 <= phase_idx < len(raw_pipeline)
                and raw_pipeline[phase_idx].get("name") == phase_str):
            probe_entry = raw_pipeline[phase_idx]
        else:
            probe_entry = next(
                (e for e in raw_pipeline if e.get("name") == phase_str), {})
        probe_names = probe_entry.get("probe_params") or []
        if probe_names:
            probe_specs = _build_probe_specs(
                probe_names, state["param_ranges"], state["centers"], sids,
                probe_entry.get("initial_radius") or {},
                state["team_config"].get("initial_radius") or {},
                locked_keys,
            )
            if probe_specs:
                print("    [probe] deterministic single-parameter probes: "
                      + ", ".join(s["label"] for s in probe_specs))
            else:
                logger.warning(
                    "probe_params %s yielded no probeable parameter; "
                    "falling back to normal MC proposals.", probe_names)

    # ── Step 1: per-worker proposals (parallel, mirrors the legacy path) ──
    def _propose_one(sid):
        worker: WorkerAgent = shared["workers"][sid]
        center = state["centers"][sid]

        wmem_data = state.get("worker_memories", {}).get(sid)
        if wmem_data:
            worker.memory = WorkerMemory.from_checkpoint(wmem_data)

        full_explore = state.get("exploration_summary", "")
        if full_explore:
            basin_lines = [ln for ln in full_explore.split("\n") if sid in ln]
            worker._exploration_summary = "\n".join(basin_lines) if basin_lines else ""
        else:
            worker._exploration_summary = ""

        guidance = None
        if prev_decision:
            guidance = shared["leader"].get_worker_guidance(prev_decision, sid)
        raw_hints = guidance.get("param_adjustments", []) if guidance else []
        direction_history = state.get("direction_history", {})
        prev_improved = False
        if round_id > 1:
            # Bundle bookkeeping: "improved" means last round's joint bundle
            # was accepted as a whole (see _swat_update_best_bundle). With no
            # bundle yet (resumed pre-bundle checkpoint), fall back to the
            # legacy per-worker ledger flag.
            best_bundle = state.get("swat_best_bundle")
            if best_bundle is not None:
                prev_improved = best_bundle.get("round_id") == round_id - 1
            else:
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

        leader_context = ""
        tokens = {"prompt": 0, "completion": 0, "total": 0}
        candidates: List[Tuple[Dict[str, float], str, str]] = []

        best_record = worker.ledger.get_best_params()
        # Trial-A provenance: with a joint bundle, the center IS the bundle's
        # params (see _swat_update_best_bundle), so the prompt's "trial A"
        # and best metrics must come from the bundle, not the per-station
        # ledger best (which may be a locally-better round whose bundle was
        # rejected). No bundle (resumed pre-bundle checkpoint): anchor stays
        # None and the legacy ledger labels/metrics apply.
        best_bundle = state.get("swat_best_bundle")
        bundle_sid_metrics = ((best_bundle or {}).get("metrics_by_sid") or {}).get(sid)
        if (best_bundle is not None
                and ((best_bundle.get("params_by_sid") or {}).get(sid))):
            worker._bundle_anchor = {
                "round_id": best_bundle.get("round_id"),
                "nse": (bundle_sid_metrics or {}).get("NSE"),
            }
        else:
            worker._bundle_anchor = None
        if worker._bundle_anchor is not None and bundle_sid_metrics:
            best_metrics = dict(bundle_sid_metrics)
        else:
            best_metrics = best_record.get("metrics", {}) if best_record else {}

        if force_best:
            # Restore the ENTIRE joint best bundle: every worker's params come
            # from the same accepted round/strategy, then run ONE joint
            # simulation. Per-worker ledger bests (below) only remain as the
            # fallback for resumed pre-bundle checkpoints without a bundle.
            best_bundle = state.get("swat_best_bundle")
            bundle_params = ((best_bundle or {}).get("params_by_sid") or {}).get(sid)
            if bundle_params:
                candidates = [(dict(bundle_params), "force_best",
                               f"force_best: joint best bundle (round "
                               f"{best_bundle.get('round_id')}, strategy "
                               f"{best_bundle.get('strategy_index')})")]
            elif best_record and best_record.get("params"):
                candidates = [(dict(best_record["params"]), "force_best",
                               "force_best: reused historical best params per basin")]
            else:
                candidates = [(dict(center), "force_best",
                               "force_best: no ledger history, used current center")]
        elif probe_specs:
            # Probe mode: no LLM calls — each strategy is the center with
            # exactly one listed param shifted by its probe delta.
            for spec in probe_specs:
                p = spec["param"]
                p_lo, p_hi = worker.ranges.get(p, (0.0, 1.0))
                cand = dict(center)
                cand[p] = (float(center.get(p, (p_lo + p_hi) / 2))
                           + spec["sign"] * spec["delta"])
                candidates.append((cand, spec["label"],
                                   f"deterministic probe of {p} "
                                   f"({'+' if spec['sign'] > 0 else '-'}"
                                   f"{spec['delta']:g} from center, no LLM call)"))
        elif use_multi:
            if guidance:
                priority = guidance.get("priority_focus", "")
                adj_hints = guidance.get("param_adjustments", [])
                hint_strs = [
                    f"{a.get('param', '?')}: {a.get('direction', '?')} ({a.get('magnitude', '')})"
                    for a in adj_hints[:4]
                ]
                leader_context = f"{priority}. Hints: {', '.join(hint_strs)}" if hint_strs else priority
            for strategy in MC_STRATEGIES[:n_candidates]:
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
                    for k in tokens:
                        tokens[k] += wtokens.get(k, 0)
                    if cand_list:
                        candidates.append((cand_list[0], strategy, reasoning))
                    else:
                        logger.warning("Worker %s MC-%s returned empty, skipping.", sid, strategy)
                except Exception as e:
                    logger.warning("Worker %s MC-%s failed (%s), skipping.", sid, strategy, e)
            if not candidates:
                logger.warning("Worker %s all MC proposals failed, using center as fallback.", sid)
                candidates = [(dict(center), "fallback", "all MC LLM calls failed; using current center")]
        elif (guidance and guidance.get("param_adjustments")) or round_id > 1:
            if guidance and guidance.get("param_adjustments"):
                priority = guidance.get("priority_focus", "")
                adj_hints = guidance.get("param_adjustments", [])
                hint_strs = [
                    f"{a.get('param', '?')}: {a.get('direction', '?')} ({a.get('magnitude', '')})"
                    for a in adj_hints[:4]
                ]
                leader_context = f"{priority}. Hints: {', '.join(hint_strs)}" if hint_strs else priority
            else:
                priority = guidance.get("priority_focus", "") if guidance else ""
                no_hint_note = ("No specific leader guidance this round; propose your "
                                "own adjustment based on your diagnosis history.")
                leader_context = f"{priority}. {no_hint_note}" if priority else no_hint_note
            cand_list, reasoning, tokens = worker.propose_params(
                round_id=round_id,
                current_center=center,
                metrics=best_metrics,
                phase=phase_str,
                leader_context=leader_context,
            )
            if cand_list:
                candidates = [(cand_list[0], "single", reasoning)]
            else:
                candidates = [(dict(center), "single", reasoning or "empty proposal; using center")]
        else:
            candidates = [(dict(center), "baseline",
                           "R1 baseline: default params (no LLM call this round)")]

        norm_cands = []
        for cand_params, label, reasoning in candidates:
            cand = dict(cand_params)
            for k in locked_keys:
                cand[k] = center.get(k, cand.get(k, 0))
            cand.update(normalize_params(cand, worker.ranges))
            norm_cands.append((cand, label, reasoning))
        return {"candidates": norm_cands, "tokens": tokens,
                "leader_context": leader_context}

    proposals: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {pool.submit(_propose_one, sid): sid for sid in sids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                proposals[sid] = future.result()
            except Exception as e:
                logger.error("Worker %s proposal failed: %s\n%s", sid, e, traceback.format_exc())
                # The joint run still needs params for this worker's HRUs;
                # its current center is the physically consistent fallback.
                proposals[sid] = {
                    "candidates": [(dict(state["centers"][sid]), "error_fallback",
                                    f"proposal failed ({e}); reused current center")],
                    "tokens": {"prompt": 0, "completion": 0, "total": 0},
                    "leader_context": "",
                }

    # ── Step 2: joint execution — ONE batched run per strategy index ──
    # apply_order is OUTER (downstream) first, INNER (upstream) last, so
    # nested gauges' HRU sets stay exclusive (see compute_exclusive_hru_sets).
    topo = _compute_topo_levels(state["upstream_map"])
    apply_order = [s for level in reversed(topo) for s in sorted(level) if s in proposals]
    for s in sids:
        if s in proposals and s not in apply_order:
            apply_order.append(s)
    outlet_id = (state["team_config"].get("outlet_subbasin_id")
                 or next((w["subbasin_id"] for w in state["worker_cfgs"]
                          if w.get("is_outlet")), ""))

    n_strategies = max(len(p["candidates"]) for p in proposals.values())
    per_strategy: List[Dict[str, Tuple[bool, Dict[str, float]]]] = []
    for k in range(n_strategies):
        per_worker_params = {}
        labels_by_sid = {}
        for sid in sids:
            cands = proposals[sid]["candidates"]
            params, label, _ = cands[min(k, len(cands) - 1)]
            per_worker_params[sid] = params
            labels_by_sid[sid] = label
        tag = f"round_{round_id}_joint_k{k+1}"
        msg = (f"SWAT batched run: {len(sids)} workers, strategy {k+1}/{n_strategies} "
               f"(round {round_id}, tag={tag})")
        print(f"    {msg}")
        logger.info(msg)
        ok, metrics_by_sid = vic_executor.run_batched(
            run_id=round_id * 100 + k + 1,
            per_worker_params=per_worker_params,
            apply_order=apply_order,
            phase_tag=tag,
            outlet_subbasin_id=outlet_id,
        )
        if ok:
            for sid in sids:
                vic_executor.copy_routing_artifacts(
                    sid,
                    round_dir / "routing_result" / sid,
                    candidate_tag=(f"c{k+1}_{labels_by_sid[sid]}" if n_strategies > 1 else ""),
                )
        per_strategy.append({sid: (ok, metrics_by_sid.get(sid, {})) for sid in sids})

    # ── Step 3: global strategy selection + per-site distribution ──
    # Each strategy k is ONE joint simulation; mixing k's across workers
    # would build a parameter field that was never simulated. Pick a single
    # basin-wide k*: k is eligible only if the joint run succeeded for every
    # worker with a finite NSE, and an eligible k scores the mean NSE across
    # all workers (matches the leader's J ≈ 0.9·mean NSE) — or the leader's
    # full J when team_config.mc_selection_metric="j_total".
    duration = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()

    sel_metric = str(state["team_config"].get("mc_selection_metric", "mean_nse"))
    if sel_metric not in ("mean_nse", "j_total"):
        logger.warning(
            "mc_selection_metric=%r is not one of ('mean_nse', 'j_total'); "
            "using 'mean_nse'.", sel_metric)
        sel_metric = "mean_nse"

    def _k_label(k: int) -> str:
        # Workers may clamp k to their own candidate list; join distinct labels
        # so the global summary never misnames k for a partially-failed worker.
        labels: List[str] = []
        for sid in sids:
            cands = proposals[sid]["candidates"]
            label = cands[min(k, len(cands) - 1)][1]
            if label not in labels:
                labels.append(label)
        return "/".join(labels)

    strategy_stats, best_stat = _swat_pick_joint_strategy(
        per_strategy, sids, sel_metric, shared["leader"].compute_global_score)
    if not best_stat["eligible"] and n_strategies > 1:
        logger.warning(
            "SWAT joint-MC: no strategy is valid on all %d sites; falling "
            "back to k=%d (%s) with %d/%d valid sites (partial mean NSE=%.4f).",
            len(sids), best_stat["idx"] + 1, _k_label(best_stat["idx"]),
            best_stat["n_valid"], len(sids), best_stat["mean_nse"])
    best_k = best_stat["idx"]

    batch_results = []
    mc_lines = []
    proposal_mode = "probe" if probe_specs else "LLM"
    if n_strategies > 1 or probe_specs:
        mean_parts = []
        for s in strategy_stats:
            part = f"k{s['idx'] + 1}({_k_label(s['idx'])})→{s['score']:.3f}"
            if not s["eligible"]:
                part += f" [{s['n_valid']}/{len(sids)} ok]"
            mean_parts.append(part)
        # The metric label doubles as the used-metric log; the default line
        # stays byte-identical for resumed runs (mc_selection_metric absent).
        metric_note = ("basin mean NSE per k: " if sel_metric == "mean_nse"
                       else "basin J_total per k (mc_selection_metric=j_total): ")
        mc_lines.append(f"joint-global: k*={best_k + 1} ({_k_label(best_k)}), "
                        + metric_note + ", ".join(mean_parts))
    _ensure_dir(round_dir / "results")
    for sid in sids:
        worker: WorkerAgent = shared["workers"][sid]
        prop = proposals[sid]
        cands = prop["candidates"]
        eval_results = []
        for k in range(n_strategies):
            params, label, reasoning = cands[min(k, len(cands) - 1)]
            ok, metrics = per_strategy[k][sid]
            nse = metrics.get("NSE", float("-inf")) if ok else float("-inf")
            score = _score_row(metrics) if ok else float("-inf")
            eval_results.append({
                "idx": k, "params": params, "metrics": metrics,
                "ok": ok, "score": score, "nse": nse, "label": label,
                "reasoning": reasoning,
            })
        # Every worker adopts the SAME joint run k*; params stay per-worker.
        best = eval_results[best_k]

        result = {
            "subbasin_id": sid,
            "station_code": worker.station_code,
            "agent_id": worker.agent_id,
            "best_params": best["params"],
            "params": best["params"],
            "metrics": best["metrics"],
            "score_local": round(best["score"], 6),
            "llm_tokens": prop["tokens"],
            "duration_sec": round(duration, 3),
        }
        result_file = {
            "round_id": round_id, "subbasin_id": sid,
            "result": {"params": best["params"], "metrics": best["metrics"],
                       "ok": best["ok"], "score_local": round(best["score"], 6)},
            "leader_context_received": prop["leader_context"],
            "llm_tokens": prop["tokens"],
            "duration_sec": round(duration, 3),
            "joint_execution": True,
        }
        if n_strategies > 1 or probe_specs:
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
            result["analysis"] = (f"MC({n_strategies},{proposal_mode},joint-global): "
                                  f"k*={best_k + 1} ({best['label']}), "
                                  f"basin_mean_NSE={best_stat['mean_nse']:.4f}")
            if sel_metric == "j_total":
                result["analysis"] += (
                    f" (selected by J_total={best_stat['score']:.4f})")
            result["mc_detail"] = mc_detail
            result["global_strategy_index"] = best_k + 1
            result["leader_context_received"] = prop["leader_context"]
            result_file["winning_strategy"] = best["label"]
            result_file["winning_reasoning"] = best.get("reasoning", "")
            result_file["global_strategy_index"] = best_k + 1
            result_file["mc_detail"] = mc_detail
            step_parts = [f"{r['label']}→{r['nse']:.3f}" for r in eval_results]
            mc_lines.append(f"{sid}: adopted={best['label']} (k*={best_k + 1}), "
                            + ", ".join(step_parts))
        else:
            result["analysis"] = "1-candidate joint run."
            result_file["reasoning"] = best.get("reasoning", "")
        _write_json(round_dir / "results" / f"worker_{sid}.json", result_file)
        batch_results.append(result)

        nse = best["metrics"].get("NSE", float("nan"))
        print(f"    Worker {sid}: NSE={nse:.4f}, score={best['score']:.4f}"
              f"{'  [MC-joint]' if n_strategies > 1 else '  [joint]'}")

    # ── Probe reporting: per-probe dNSE vs each site's center baseline ──
    # (centers are pinned to the accepted joint bundle when one exists, so
    # the baseline is that bundle's per-site NSE; with no bundle (resumed
    # pre-bundle checkpoint), fall back to the per-worker ledger best, whose
    # metric IS the center's on that legacy path; without either, unknown).
    if probe_specs:
        swat_cfg0 = next(
            iter(getattr(vic_executor, "_vic_configs", {}).values()), None)
        change_types = dict(getattr(swat_cfg0, "change_types", {}) or {}) \
            if swat_cfg0 is not None else {}
        best_bundle = state.get("swat_best_bundle")
        bundle_metrics = (best_bundle or {}).get("metrics_by_sid") or {}
        baseline_nse: Dict[str, Optional[float]] = {}
        baseline_source: Dict[str, str] = {}
        for sid in sids:
            b = (bundle_metrics.get(sid) or {}).get("NSE")
            if b is not None:
                baseline_source[sid] = (
                    f"joint best bundle (round {best_bundle.get('round_id')})")
            else:
                rec = shared["workers"][sid].ledger.get_best_params()
                b = rec.get("nse") if rec else None
                baseline_source[sid] = "per-station ledger best (no bundle)"
            baseline_nse[sid] = (
                float(b) if b is not None and math.isfinite(float(b)) else None)
        base_parts = []
        for sid in sids:
            site = shared["workers"][sid].station_code or sid
            b = baseline_nse[sid]
            base_parts.append(
                f"{site}={b:.3f}" if b is not None else f"{site}=?")
        src_label = "; ".join(sorted(set(baseline_source.values())))
        mc_lines.append(f"probe baseline [{src_label}]: "
                        + ", ".join(base_parts))
        probe_responses = []
        for k, spec in enumerate(probe_specs):
            sign_char = "+" if spec["sign"] > 0 else "-"
            ctype = change_types.get(spec["param"], "")
            head = f"probe {spec['param']} {sign_char}{spec['delta']:g}"
            if ctype:
                head += f" ({ctype})"
            parts = []
            deltas = []
            responses = {}
            for sid in sids:
                ok, metrics = per_strategy[k][sid]
                p_nse = metrics.get("NSE", float("nan")) if ok else float("nan")
                site = shared["workers"][sid].station_code or sid
                base = baseline_nse[sid]
                if not math.isfinite(p_nse):
                    parts.append(f"{site} dNSE=? (run failed)")
                    responses[sid] = {"nse": None, "dNSE": None, "ok": ok,
                                      "note": "run failed"}
                elif base is None:
                    parts.append(f"{site} NSE={p_nse:.3f} (baseline unknown)")
                    responses[sid] = {"nse": p_nse, "dNSE": None, "ok": ok,
                                      "note": "baseline unknown"}
                else:
                    d = p_nse - base
                    deltas.append(d)
                    parts.append(f"{site} dNSE={d:+.2f}")
                    responses[sid] = {"nse": p_nse, "dNSE": d, "ok": ok}
            if len(deltas) < len(sids) or not deltas:
                verdict = "UNKNOWN"
            elif all(d < 0 for d in deltas):
                verdict = "NEGATIVE"
            elif all(d > 0 for d in deltas):
                verdict = "POSITIVE"
            else:
                verdict = "MIXED"
            tail = f"-> direction {verdict}"
            if verdict in ("NEGATIVE", "POSITIVE") and len(deltas) > 1:
                tail += " for both" if len(deltas) == 2 else " for all"
            line = f"{head}: {', '.join(parts)} {tail}"
            mc_lines.append(line)
            probe_responses.append({
                "strategy_index": k + 1,
                "param": spec["param"],
                "delta": spec["delta"],
                "direction": sign_char,
                "label": spec["label"],
                "change_type": ctype,
                "responses": responses,
                "verdict": verdict,
                "summary": line,
            })
        _write_json(round_dir / "results" / "probe_responses.json", _to_native({
            "round_id": round_id,
            "phase": phase_str,
            "baseline_nse": baseline_nse,
            "baseline_source": baseline_source,
            "probe_responses": probe_responses,
        }))

    mc_summaries = dict(state.get("mc_round_summaries", {}))
    if mc_lines:
        prev = mc_summaries.get(round_id, "")
        mc_summaries[round_id] = (prev + "\n" + "\n".join(mc_lines)).strip()

    return _to_native({
        "batch_worker_results": batch_results,
        "mc_round_summaries": mc_summaries,
    })


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
    locked_keys = set(get_locked_keys(
        CalibrationPhase(state["current_phase_name"]),
        model_combination=state.get("model_combination", "vic_routing"),
    ))
    param_ranges = state["param_ranges"]

    # Update latest_best and centers
    latest_best = dict(state.get("leader_latest_best_by_subbasin", {}))
    shared = _get_shared(state)

    # SWAT rounds run the whole watershed as ONE joint simulation, so centers
    # move on watershed-level bundle acceptance instead of per-worker NSE
    # improvement (mixed-round centers would be a parameter field that was
    # never simulated). Requires the batch to cover every worker; a resumed
    # pre-collapse checkpoint with partial batches degrades to the legacy
    # per-worker updates with a warning.
    swat_joint = False
    if state.get("model_combination", "vic_routing") == "swat" and batch_results:
        all_sids = {w["subbasin_id"] for w in state["worker_cfgs"]}
        batch_sids = {r["subbasin_id"] for r in batch_results}
        swat_joint = batch_sids == all_sids
        if not swat_joint:
            logger.warning(
                "SWAT joint bundle: batch covers %d/%d workers; falling back "
                "to legacy per-worker center updates for this batch.",
                len(batch_sids), len(all_sids))

    for result in batch_results:
        bsid = result["subbasin_id"]
        bnse = result.get("metrics", {}).get("NSE", float("-inf"))
        worker = shared["workers"].get(bsid)

        # Restore worker memory for ledger
        wmem_data = state.get("worker_memories", {}).get(bsid)
        if wmem_data and worker:
            worker.memory = WorkerMemory.from_checkpoint(wmem_data)

        if not swat_joint:
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

    update = {
        "round_worker_results": round_results,
        "centers": centers,
        "leader_latest_best_by_subbasin": latest_best,
        "batch_worker_results": [],  # clear for next batch
    }
    if swat_joint:
        bundle, accepted = _swat_update_best_bundle(
            state, batch_results, centers,
            shared["leader"].compute_global_score)
        update["swat_best_bundle"] = bundle
        if bundle is not None:
            print(f"    [bundle] round {state['round_id']} joint bundle "
                  f"{'ACCEPTED' if accepted else 'rejected'}; best=round "
                  f"{bundle.get('round_id')} k={bundle.get('strategy_index')} "
                  f"(mean_NSE={bundle.get('mean_nse', float('nan')):.4f}, "
                  f"J_total={bundle.get('j_total', float('nan')):.4f})")
    return _to_native(update)


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
            context={"is_headwater": is_headwater, "upstream_quality": upstream_quality,
                     "model_combination": worker.model_combination,
                     "current_params": result.get("params") or result.get("best_params") or {}},
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
            # Winner idx: SWAT joint path keeps mc_detail k-ordered and records
            # the adopted k as global_strategy_index; the legacy MC path sorts
            # mc_detail winner-first, so its first entry is the winner.
            winner_idx = wr.get("global_strategy_index", mc_detail[0]["idx"])
            for cand in mc_detail:
                cand_metrics = cand.get("metrics") or {}
                if not cand_metrics:
                    continue
                cand_diag = diagnose_round(
                    cand_metrics, prev_metrics,
                    {"is_headwater": is_headwater, "upstream_quality": up_q,
                     "model_combination": worker.model_combination,
                     "current_params": cand.get("params") or {}},
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
                    "is_winner": idx == winner_idx,
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

    import math

    def _f(v, fmt=".4f"):
        if v is None:
            return "N/A"
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                return "N/A"
            return format(f, fmt)
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
            expected_subbasins=[w["subbasin_id"] for w in state["worker_cfgs"]],
        )

    # Call LLM (stateless — single-turn, no conversation history)
    print(f"\n  Leader reviewing round {round_id}...")
    decision = None
    leader_tokens = {"prompt": 0, "completion": 0, "total": 0}
    try:
        llm_resp = leader.llm.chat(
            messages=[{"role": "user", "content": review_prompt}],
            system=system_prompt,
            json_schema=leader.output_schema,
        )
        leader_tokens = {
            "prompt": llm_resp.prompt_tokens,
            "completion": llm_resp.completion_tokens,
            "total": llm_resp.total_tokens,
        }
        decision = llm_resp.parsed
        if decision is not None:
            validation = validate_leader_output(
                decision,
                expected_subbasins=[w["subbasin_id"] for w in state["worker_cfgs"]],
                allowed_params=list(state["param_ranges"].keys()),
            )
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
    model_combination = state.get("model_combination", "vic_routing")
    locked_keys = set(get_locked_keys(
        phase, model_combination=model_combination,
    ))
    param_ranges = state["param_ranges"]

    centers = dict(state["centers"])
    radii = dict(state["radii"])
    direction_history = dict(state.get("direction_history", {}))

    # SWAT centers follow the joint bundle set in collect_batch_results;
    # per-worker NSE improvement must not move them here (that would
    # rebuild the mixed-round parameter field the bundle exists to prevent).
    swat_bundle = (state.get("swat_best_bundle")
                   if model_combination == "swat" else None)
    swat_bundle_accepted = (swat_bundle is not None
                            and swat_bundle.get("round_id") == state["round_id"])

    result_by_sid = {r["subbasin_id"]: r for r in worker_results}
    for sid in workers:
        result = result_by_sid.get(sid)
        if not result:
            continue

        new_nse = result.get("metrics", {}).get("NSE", float("-inf"))
        worker_best = workers[sid].ledger.get_best_nse()

        if model_combination != "swat" and new_nse >= worker_best:
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
            if hint not in ("shrink", "expand", "keep"):
                # The leader schema also allows hold/maintain as keep
                # synonyms; normalize them here. Any other unknown value
                # degrades to keep so radii are never silently corrupted.
                if str(hint).strip().lower() in ("hold", "maintain"):
                    hint = "keep"
                else:
                    logger.debug(
                        "Unknown search_radius_hint %r for %s; treating as 'keep'.",
                        hint, sid)
                    hint = "keep"
            if hint == "shrink":
                radii[sid] = {k: v * 0.8 for k, v in radii[sid].items()}
            elif hint == "expand":
                radii[sid] = {
                    k: min(v * 1.3, (param_ranges.get(k, (0, 1))[1] - param_ranges.get(k, (0, 1))[0]) * 0.5)
                    for k, v in radii[sid].items()
                }

        # Anti-flip direction update. SWAT improvement is watershed-level:
        # this round's joint bundle was accepted as a whole.
        raw_hints = guidance.get("param_adjustments", []) if guidance else []
        if model_combination == "swat":
            improved = swat_bundle_accepted
        else:
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
    # SWAT: note which joint bundle is the current watershed-level best
    # (ledgers keep their per-worker entries above; they just no longer
    # drive swat centers).
    if state.get("model_combination", "vic_routing") == "swat":
        bundle = state.get("swat_best_bundle")
        if bundle is not None:
            meta["swat_best_bundle"] = {
                "round_id": bundle.get("round_id"),
                "strategy_index": bundle.get("strategy_index"),
                "j_total": bundle.get("j_total"),
                "mean_nse": bundle.get("mean_nse"),
                "accepted_this_round": bundle.get("round_id") == round_id,
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

    # SWAT patience follows joint-bundle acceptance — the same criterion
    # that moves centers (see _swat_update_best_bundle) — so patience and
    # centers can never diverge. Other combinations (and swat rounds before
    # any bundle exists, e.g. a resumed pre-bundle checkpoint) keep the
    # legacy mean_sub_nse plateau rule unchanged.
    swat_bundle = (state.get("swat_best_bundle")
                   if state.get("model_combination", "vic_routing") == "swat"
                   else None)
    if swat_bundle is not None:
        improved_this_round = swat_bundle.get("round_id") == state.get("round_id")
    else:
        improved_this_round = latest_msn > best_in_phase
    if latest_msn > best_in_phase:
        best_in_phase = latest_msn
    if improved_this_round:
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
            executor: ModelExecutor = shared["vic_executor"]
            output_root = Path(state["output_root"])
            # Land-surface → routing transition: cache the land-surface output
            # so routing-only rounds can skip rerunning it. Generic phase-name
            # matching here covers VIC_ONLY → ROUTING_ONLY (vic_routing combo)
            # and VIC_ONLY → CAMA_FLOOD_ONLY (vic_cama_flood combo).
            land_to_routing = old_name == "VIC_ONLY" and new_name in (
                "ROUTING_ONLY", "CAMA_FLOOD_ONLY",
            )
            routing_to_joint = old_name in (
                "ROUTING_ONLY", "CAMA_FLOOD_ONLY",
            ) and new_name == "JOINT"
            if land_to_routing:
                executor.cache_intermediates_for_routing(
                    state["centers"], output_root, state, state["worker_cfgs"],
                )
            elif routing_to_joint:
                executor.clear_intermediate_cache()

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


# NOTE: _cache_vic_fluxes_for_routing / _clear_vic_flux_cache previously lived
# here; they moved to scripts/executors/vic_routing.py as part of the
# ModelExecutor refactor (2026-06-23). graph_nodes now calls
# executor.cache_intermediates_for_routing / clear_intermediate_cache.


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
        "optimization_max_rounds": state.get("optimization_max_rounds", 0),
        "final_round_is_additional": True,
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
