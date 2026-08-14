#!/usr/bin/env python3
"""wflow_sbm model execution wrapper.

Provides a clean interface for running wflow_sbm (Wflow.jl, with integrated
kinematic-wave routing), mirroring the SWAT+ executor API so the LangGraph
nodes need only an additive ``"wflow"`` branch. Supports real and simulate
modes.

In simulate mode no Wflow.jl binary or LLM is required.

wflow_sbm mirrors the SWAT+ integration EXACTLY: one integrated joint
simulation per round, with each worker's parameters scoped to a disjoint set
of grid cells (nested cell locking, analogous to SWAT's HRU locking). It is a
SINGLE calibration phase ``WFLOW_SBM`` (like SWAT's single ``SWATPLUS_HRU``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .routing_utils import UpstreamRoutingInfo

logger = logging.getLogger(__name__)

# wflow_sbm calibration parameters. Eight knobs following the official wflow_sbm
# documentation, spanning subsurface flow, infiltration/baseflow magnitude,
# recession shape, soil storage, overland + channel routing, and snow
# accumulation/melt (essential in snow-dominated basins with a spring freshet).
WFLOW_PARAM_KEYS = [
    "KsatHorFrac",   # horizontal/vertical Ksat ratio (lateral subsurface flow)
    "KsatVer",       # vertical saturated conductivity (infiltration / baseflow)
    "f",             # exponential KsatVer decay with depth (recession shape)
    "SoilThickness", # soil storage depth (storage / delayed response)
    "N",             # land Manning n (overland resistance; peak damping/timing)
    "N_River",       # river Manning n (channel wave attenuation; flood timing)
    "TT",            # snowfall temperature threshold (snow accumulation; freshet size)
    "Cfmax",         # degree-day snowmelt coefficient (melt rate; freshet timing)
]

# wflow_sbm change-types. "mult" = multiply the base staticmaps map cell-wise
# by the proposed multiplier (preserves the spatial heterogeneity of the
# gridded map). "absval" = set the map cell to exactly the proposed value
# (used for parameters that live as a scalar `.value` in the base config
# today, so there is no per-cell map to preserve).
CHANGE_TYPES: Dict[str, str] = {
    "KsatHorFrac":   "absval",   # scalar today -> set per-cell value
    "KsatVer":       "mult",     # gridded map -> multiplier preserves pattern
    "f":             "mult",     # gridded map -> multiplier preserves pattern
    "SoilThickness": "mult",     # gridded map -> multiplier preserves pattern
    "N":             "absval",   # scalar today -> set per-cell value (land)
    "N_River":       "absval",   # scalar today -> set per-cell value (river)
    "TT":            "absval",   # default-only today -> set per-cell value (snow)
    "Cfmax":         "absval",   # default-only today -> set per-cell value (snow)
}


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Simulate mode: deterministic pseudo-metrics (no Wflow.jl execution, no API)
# ---------------------------------------------------------------------------

def simulate_metrics(
    subbasin_id: str,
    round_id: int,
    candidate_id: int,
    params: Dict[str, float],
) -> Dict[str, float]:
    """Generate deterministic pseudo-metrics for testing the LLM agent flow.

    A smooth response surface anchored on plausibly-optimal wflow_sbm
    parameter values, plus deterministic noise. Mirrors the SWAT+ simulate
    path.
    """
    seed = f"{subbasin_id}-{round_id}-{candidate_id}-{json.dumps(params, sort_keys=True)}"

    nse = 0.55
    khf = float(params.get("KsatHorFrac", 100.0))
    nse += 0.08 * (1.0 - abs(khf - 200.0) / 800.0)
    nse += 0.05 * (1.0 - abs(float(params.get("KsatVer", 1.0)) - 1.0) / 3.0)
    nse += 0.04 * (1.0 - abs(float(params.get("f", 1.0)) - 1.0) / 1.5)
    nse += 0.04 * (1.0 - abs(float(params.get("SoilThickness", 1.0)) - 1.0) / 1.5)
    nse += 0.03 * (1.0 - abs(float(params.get("N", 0.15)) - 0.15) / 0.46)
    nse += 0.03 * (1.0 - abs(float(params.get("N_River", 0.075)) - 0.075) / 0.125)
    nse += _seeded_noise(seed + "-nse", -0.03, 0.03)
    nse = clamp(nse, -0.2, 0.95)

    rmse = 900 - 260 * nse + _seeded_noise(seed + "-rmse", -12, 12)
    pbias = _seeded_noise(seed + "-pbias", -14, 14)
    peak_ratio = _seeded_noise(seed + "-peak", 0.85, 1.15)
    lowflow_ratio = _seeded_noise(seed + "-lowf", 0.7, 1.5)

    kge = nse + _seeded_noise(seed + "-kge", -0.08, 0.05)
    kge = clamp(kge, -0.3, 0.95)

    r2 = clamp(nse + _seeded_noise(seed + "-r2", -0.05, 0.08), -0.2, 0.99)

    return {
        "NSE": round(nse, 4),
        "KGE": round(kge, 4),
        "RMSE": round(rmse, 2),
        "PBIAS": round(pbias, 2),
        "R2": round(r2, 4),
        "peak_ratio": round(peak_ratio, 3),
        "lowflow_ratio": round(lowflow_ratio, 3),
    }


def _seeded_noise(key: str, lo: float, hi: float) -> float:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    u = int(h[:12], 16) / float(16**12 - 1)
    return lo + (hi - lo) * u


# ---------------------------------------------------------------------------
# Real mode: wflow_sbm via wflow_runner
# ---------------------------------------------------------------------------

@dataclass
class WflowConfig:
    """Configuration for real wflow_sbm execution per gauge/sub-basin.

    wflow_sbm has integrated routing (kinematic wave in the channel), so
    discharge per gauge is read directly from the model's gauge-discharge CSV
    (one column per gauge id). Each worker's parameters are applied to a
    disjoint set of grid cells (nested cell locking).
    """
    basin_name: str
    base_output_dir: str            # working scratch root
    wflow_model_dir: str            # path to the read-only wflow model dir
    wflow_run_cmd: str              # Julia command template that runs Wflow.jl
    obs_file: str                   # observed Q file (date,Q_m3s)
    cell_ids: List[int] = field(default_factory=list)  # linear grid-cell ids draining to this gauge
    subbasin_attrs_file: str = ""   # JSON listing all cells (cell_id, area, subcatch)
    gauge_id: int = 0               # gauge id in wflow_gauges / CSV column Q_<gauge_id>
    start_date: str = "1987-01-01"  # simulation (and metric) start date
    end_date: str = "2000-12-31"
    change_types: Dict[str, str] = field(default_factory=lambda: dict(CHANGE_TYPES))

    def validate(self) -> List[str]:
        missing = []
        for attr in ("wflow_model_dir", "obs_file"):
            path = getattr(self, attr, "")
            if path and not os.path.exists(path):
                missing.append(f"{attr}: {path}")
        saf = getattr(self, "subbasin_attrs_file", "")
        if saf and not os.path.exists(saf):
            missing.append(f"subbasin_attrs_file: {saf}")
        if not self.cell_ids:
            missing.append(f"cell_ids: empty list for {self.basin_name}")
        return missing


def run_wflow_real(
    wflow_config: WflowConfig,
    run_id: int,
    params: Dict[str, float],
    phase_tag: str,
    log_path: str,
    output_csv_path: Optional[str] = None,
    cleanup: bool = True,
) -> Tuple[bool, Dict[str, float]]:
    """Run real wflow_sbm and return metrics (see wflow_runner.run_once)."""
    try:
        from . import wflow_runner

        ok, metrics = wflow_runner.run_once(
            cfg=wflow_config,
            run_id=run_id,
            params=params,
            phase_tag=phase_tag,
            log_path=log_path,
            output_csv_path=output_csv_path,
            cleanup=cleanup,
        )
        if not ok:
            metrics = _nan_metrics()
        return ok, metrics
    except Exception as e:
        logger.error("wflow execution failed for %s: %s", wflow_config.basin_name, e)
        import traceback
        logger.error(traceback.format_exc())
        return False, _nan_metrics()


def _nan_metrics() -> Dict[str, float]:
    return {
        "NSE": float("nan"),
        "KGE": float("nan"),
        "RMSE": float("nan"),
        "PBIAS": float("nan"),
        "R2": float("nan"),
        "peak_ratio": float("nan"),
        "lowflow_ratio": float("nan"),
    }


# ---------------------------------------------------------------------------
# Unified executor (drop-in replacement for SWATPlusExecutor)
# ---------------------------------------------------------------------------

class WflowExecutor:
    """wflow_sbm executor: dispatches to simulate or real mode.

    The public API matches SWATPlusExecutor so graph_nodes drives it through
    the same joint-batched dispatch (run_batched) with no per-worker changes.
    """

    def __init__(self, mode: str = "simulate", cleanup: bool = True):
        if mode not in ("simulate", "real"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'simulate' or 'real'.")
        self.mode = mode
        self.cleanup = cleanup
        # Name kept as _vic_configs for symmetry with the SWAT/VIC executors
        # (graph_nodes references this attribute generically).
        self._vic_configs: Dict[str, WflowConfig] = {}
        self._last_series: Dict[str, Tuple[Dict[str, float], Dict[str, float]]] = {}

    def register_basin(self, subbasin_id: str, wflow_config: WflowConfig):
        """Register a wflow configuration for a sub-basin (real mode only)."""
        self._vic_configs[subbasin_id] = wflow_config

    def run(
        self,
        subbasin_id: str,
        run_id: int,
        candidate_id: int,
        params: Dict[str, float],
        phase_tag: str = "",
        log_path: str = "",
        upstream_routing_info: Optional[List[UpstreamRoutingInfo]] = None,
        upstream_soil_info: Optional[List[Any]] = None,
        output_csv_path: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, float]]:
        """Execute wflow_sbm (or simulation) for one parameter set.

        wflow_sbm has integrated routing, so upstream_* kwargs are accepted for
        interface compatibility but ignored. Real single-worker runs apply the
        params to the whole model domain (fallback; the normal path is
        run_batched).
        """
        if self.mode == "simulate":
            metrics = simulate_metrics(subbasin_id, run_id, candidate_id, params)
            return True, metrics
        else:
            wflow_cfg = self._vic_configs.get(subbasin_id)
            if wflow_cfg is None:
                logger.error("No wflow config registered for basin: %s", subbasin_id)
                return False, _nan_metrics()
            tag = phase_tag or f"round_{run_id}_c{candidate_id}"
            log = log_path or f"/tmp/wflow_{subbasin_id}_{run_id}_{candidate_id}.log"
            return run_wflow_real(
                wflow_cfg, run_id, params, tag, log,
                output_csv_path=output_csv_path,
                cleanup=self.cleanup,
            )

    def get_last_series(
        self, subbasin_id: str
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Return the most recent (sim, obs) daily dicts for *subbasin_id*."""
        return self._last_series.get(subbasin_id, ({}, {}))

    def run_batched(
        self,
        run_id: int,
        per_worker_params: Dict[str, Dict[str, float]],
        apply_order: List[str],
        phase_tag: str = "",
        log_path: str = "",
        outlet_subbasin_id: str = "tmz",
        output_csv_paths: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, Dict[str, Dict[str, float]]]:
        """Run ONE wflow_sbm simulation applying every worker's params at once.

        In real mode this opens a single model-dir copy, writes one per-run
        staticmaps.nc with disjoint cell selectors per worker (see
        :func:`wflow_runner.compute_exclusive_hru_sets`), runs Wflow.jl via
        Julia, then reads per-gauge daily Q from the gauge CSV. Per-worker
        (sim, obs) series are stashed in :pyattr:`_last_series`.

        In simulate mode it loops through workers and calls
        :func:`simulate_metrics` for each (no subprocess).
        """
        if self.mode == "simulate":
            metrics_by_sid: Dict[str, Dict[str, float]] = {}
            for sid, params in per_worker_params.items():
                metrics_by_sid[sid] = simulate_metrics(sid, run_id, 1, params)
                self._last_series.pop(sid, None)
            return True, metrics_by_sid

        base_cfg = self._vic_configs.get(outlet_subbasin_id)
        if base_cfg is None:
            if not self._vic_configs:
                logger.error("No wflow configs registered for batched run.")
                nan = _nan_metrics()
                return False, {sid: dict(nan) for sid in per_worker_params}
            base_cfg = next(iter(self._vic_configs.values()))

        from . import wflow_runner as _wr
        worker_specs: List[_wr.WorkerSpec] = []
        for sid, cfg in self._vic_configs.items():
            if sid in per_worker_params:
                worker_specs.append(_wr.WorkerSpec(
                    subbasin_id=sid,
                    hru_fids=list(cfg.cell_ids),
                    obs_file=cfg.obs_file,
                    start_date=cfg.start_date,
                    end_date=cfg.end_date,
                    gauge_id=int(getattr(cfg, "gauge_id", 0) or 0),
                ))

        tag = phase_tag or f"batched_r{run_id}"
        log = log_path or f"/tmp/wflow_batched_{run_id}.log"

        ok, metrics_by_sid, sim_by_sid, obs_by_sid = _wr.run_once_batched(
            base_cfg=base_cfg,
            run_id=run_id,
            per_worker_params=per_worker_params,
            worker_specs=worker_specs,
            apply_order=apply_order,
            phase_tag=tag,
            log_path=log,
            output_csv_paths=output_csv_paths,
            change_types=base_cfg.change_types,
            cleanup=self.cleanup,
        )

        for sid in per_worker_params:
            sim = sim_by_sid.get(sid, {})
            obs = obs_by_sid.get(sid, {})
            self._last_series[sid] = (sim, obs)

        if not ok:
            nan = _nan_metrics()
            return False, {sid: dict(nan) for sid in per_worker_params}
        return True, metrics_by_sid

    def validate_all(self) -> Dict[str, List[str]]:
        """Validate all registered wflow configs. Returns {basin: [missing]}."""
        issues = {}
        for sid, cfg in self._vic_configs.items():
            missing = cfg.validate()
            if missing:
                issues[sid] = missing
        return issues
