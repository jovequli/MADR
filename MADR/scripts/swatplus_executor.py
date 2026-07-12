#!/usr/bin/env python3
"""SWAT+ model execution wrapper.

Provides a clean interface for running SWAT+ (with integrated routing),
mirroring the original VIC executor API so the LangGraph nodes do not need
changes. Supports both real and simulate modes.

In simulate mode no SWAT+ binary or LLM is required.
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

# SWAT+ calibration parameters (v2: names verified against cal_parms.cal).
# Removed: gw_delay (source-code disabled), alpha_bf (wrong name; use 'alpha').
# Added: revap_min, deep_seep, and 5 snow params (TNH-relevant).
PARAM_KEYS = [
    "cn2", "esco", "awc", "surlag", "lat_ttime", "canmx",
    "epco", "perco", "alpha", "revap_co", "revap_min", "flo_min", "deep_seep",
    "snofall_tmp", "snomelt_tmp", "snomelt_max", "snomelt_min", "snomelt_lag",
]

# SWAT+ change-types per the guide section 2.2
# All names below have been verified against the model's cal_parms.cal.
# Removed: gw_delay (source-code disabled), alpha_bf (wrong name; use 'alpha').
# Added: revap_min, deep_seep, and 5 snow parameters (TNH only at the prompt
# level but globally tunable here so the runner accepts them).
CHANGE_TYPES: Dict[str, str] = {
    "cn2":          "pctchg",   # preserve spatial pattern from soil/landuse
    "esco":         "absval",
    "awc":          "pctchg",
    "surlag":       "absval",
    "lat_ttime":    "absval",
    "canmx":        "absval",
    "epco":         "absval",
    "perco":        "absval",
    "alpha":        "absval",   # NOTE: was alpha_bf in v1; cal_parms.cal has 'alpha'.
    "revap_co":     "absval",
    "revap_min":    "absval",   # water-table depth threshold for revap (m)
    "flo_min":      "absval",   # water-table depth threshold for baseflow (m)
    "deep_seep":    "absval",   # deep percolation fraction (m/m)
    "snofall_tmp":  "absval",   # snow params (TNH only at prompt level)
    "snomelt_tmp":  "absval",
    "snomelt_max":  "absval",
    "snomelt_min":  "absval",
    "snomelt_lag":  "absval",
}


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Simulate mode: deterministic pseudo-metrics (no SWAT+ execution, no API)
# ---------------------------------------------------------------------------

def simulate_metrics(
    subbasin_id: str,
    round_id: int,
    candidate_id: int,
    params: Dict[str, float],
) -> Dict[str, float]:
    """Generate deterministic pseudo-metrics for testing the LLM agent flow.

    Adapted from the VIC version: a smooth response surface anchored on
    plausibly-optimal SWAT+ parameter values, plus deterministic noise.
    """
    seed = f"{subbasin_id}-{round_id}-{candidate_id}-{json.dumps(params, sort_keys=True)}"

    # Optimum-anchored NSE response. Centers are reasonable for Huaihe.
    nse = 0.55
    # cn2 is a PERCENT CHANGE (pctchg, range [-25, 25], default 0); optimum
    # at a small positive offset, degrading smoothly toward both bounds.
    nse += 0.08 * (1.0 - abs(params.get("cn2", 0.0) - 5.0) / 30.0)
    nse += 0.05 * (1.0 - abs(params.get("esco", 0.95) - 0.95))
    nse += 0.04 * (1.0 - abs(params.get("awc", 0.0) - 0.0) / 50.0)
    nse += 0.04 * (1.0 - abs(params.get("surlag", 4.0) - 4.0) / 12.0)
    nse += 0.03 * (1.0 - abs(params.get("alpha", 0.05) - 0.05) / 1.0)
    nse += 0.03 * (1.0 - abs(params.get("flo_min", 3.0) - 3.0) / 20.0)
    nse += 0.02 * (1.0 - abs(params.get("deep_seep", 0.05) - 0.05) / 0.4)
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
# Real mode: SWAT+ via swatplus_runner
# ---------------------------------------------------------------------------

@dataclass
class SWATPlusConfig:
    """Configuration for real SWAT+ execution per gauge/sub-basin.

    The SWAT+ template uses HRU-only objects (no channel objects), so
    discharge per gauge is aggregated from hru_wb_day.txt using each
    gauge's HRU fids and area mapping (subbasin_attrs.json).
    """
    basin_name: str
    base_output_dir: str           # working scratch root
    txtinout_template: str         # path to read-only TxtInOut template
    swatplus_exe: str              # path to the swatplus binary
    obs_file: str                  # observed Q file (real Huaihe format)
    hru_fids: List[int] = field(default_factory=list)  # HRU FIDs that drain to this gauge
    subbasin_attrs_file: str = ""  # JSON listing all HRUs in SWAT+ unit order (FID, area_km2)
    channel_id: int = 0            # kept for backward-compat; unused with HRU-only template
    start_date: str = "1980-01-01"  # evaluation start date (after warmup)
    end_date: str = "1990-12-31"
    change_types: Dict[str, str] = field(default_factory=lambda: dict(CHANGE_TYPES))

    def validate(self) -> List[str]:
        missing = []
        for attr in ("txtinout_template", "obs_file", "subbasin_attrs_file"):
            path = getattr(self, attr, "")
            if path and not os.path.exists(path):
                missing.append(f"{attr}: {path}")
        if self.swatplus_exe and not os.path.exists(self.swatplus_exe):
            missing.append(f"swatplus_exe: {self.swatplus_exe}")
        if not self.hru_fids:
            missing.append(f"hru_fids: empty list for {self.basin_name}")
        return missing


def run_swatplus_real(
    swat_config: SWATPlusConfig,
    run_id: int,
    params: Dict[str, float],
    phase_tag: str,
    log_path: str,
    output_csv_path: Optional[str] = None,
    cleanup: bool = True,
) -> Tuple[bool, Dict[str, float]]:
    """Run real SWAT+ and return metrics.

    Steps:
    1. Copy TxtInOut template to a per-run working directory.
    2. Write calibration.cal with the 12 params and their change-types.
    3. Run the SWAT+ binary in the working dir.
    4. Parse hru_wb_day.txt and aggregate per-gauge daily discharge.
    5. Compare to obs_file and compute NSE/KGE/PBIAS/RMSE/R2.
    6. If `output_csv_path` is given, write per-day `date,Q_sim,Q_obs` CSV.
    7. If `cleanup` is True (default), delete the per-run TxtInOut scratch.
    """
    try:
        from . import swatplus_runner

        ok, metrics = swatplus_runner.run_once(
            cfg=swat_config,
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
        logger.error("SWAT+ execution failed for %s: %s", swat_config.basin_name, e)
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
# Unified executor (drop-in replacement for VICExecutor)
# ---------------------------------------------------------------------------

class SWATPlusExecutor:
    """SWAT+ executor: dispatches to simulate or real mode.

    The public API matches VICExecutor so graph_nodes does not need changes
    beyond renaming imports.
    """

    def __init__(self, mode: str = "simulate", cleanup: bool = True):
        if mode not in ("simulate", "real"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'simulate' or 'real'.")
        self.mode = mode
        # cleanup=True deletes per-run TxtInOut scratch after metrics are
        # computed; set False to keep scratch for debugging.
        self.cleanup = cleanup
        # Name kept as _vic_configs intentionally for backward-compat with
        # any helper that still references this attribute in graph_nodes.
        self._vic_configs: Dict[str, SWATPlusConfig] = {}
        # Per-worker daily series cache, populated by run_batched so the
        # graph node can dump per-worker discharge CSVs without changing
        # the existing run() return signature. Each entry is
        # (sim_by_date, obs_by_date) where keys are 'YYYY-MM-DD' strings.
        self._last_series: Dict[str, Tuple[Dict[str, float], Dict[str, float]]] = {}

    def register_basin(self, subbasin_id: str, swat_config: SWATPlusConfig):
        """Register a SWAT+ configuration for a sub-basin (real mode only)."""
        self._vic_configs[subbasin_id] = swat_config

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
        """Execute SWAT+ (or simulation) for one parameter set.

        SWAT+ has integrated routing, so upstream_* kwargs are accepted for
        interface compatibility but ignored.

        Parameters
        ----------
        output_csv_path : optional
            If provided (real mode only), the daily `date,Q_sim,Q_obs` time
            series is written to this path covering the full simulation
            period configured on the basin.
        """
        if self.mode == "simulate":
            metrics = simulate_metrics(subbasin_id, run_id, candidate_id, params)
            return True, metrics
        else:
            swat_cfg = self._vic_configs.get(subbasin_id)
            if swat_cfg is None:
                logger.error("No SWAT+ config registered for basin: %s", subbasin_id)
                return False, _nan_metrics()
            tag = phase_tag or f"round_{run_id}_c{candidate_id}"
            log = log_path or f"/tmp/swatplus_{subbasin_id}_{run_id}_{candidate_id}.log"
            return run_swatplus_real(
                swat_cfg, run_id, params, tag, log,
                output_csv_path=output_csv_path,
                cleanup=self.cleanup,
            )

    def get_last_series(
        self, subbasin_id: str
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Return the most recent (sim, obs) daily dicts for *subbasin_id*.

        Keys are 'YYYY-MM-DD' strings, values are m3/s. Returns ``({}, {})``
        if no series is cached (e.g. simulate mode or a failed run).
        """
        return self._last_series.get(subbasin_id, ({}, {}))

    def run_batched(
        self,
        run_id: int,
        per_worker_params: Dict[str, Dict[str, float]],
        apply_order: List[str],
        phase_tag: str = "",
        log_path: str = "",
        outlet_subbasin_id: str = "bengbu",
        output_csv_paths: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, Dict[str, Dict[str, float]]]:
        """Run ONE SWAT+ simulation that applies every worker's params at once.

        In real mode this opens a single TxtInOut copy, writes one
        calibration.cal with disjoint HRU selectors per worker (see
        :func:`swatplus_runner.compute_exclusive_hru_sets`), runs the SWAT+
        binary, then aggregates per-gauge daily Q from ``hru_wb_day.txt``.
        The per-worker (sim, obs) series are stashed in
        :pyattr:`_last_series` so callers can dump CSVs.

        In simulate mode it loops through workers and calls
        :func:`simulate_metrics` for each. No SWAT+ subprocess runs and the
        per-worker series cache is cleared.

        Parameters
        ----------
        run_id
            Identifier used in the log filename / phase tag.
        per_worker_params
            ``{subbasin_id: {param: value}}``. Order is preserved by
            *apply_order*.
        apply_order
            Application order: outer catchments first, innermost last.
            Inner gauges' HRU sets are kept intact; outer gauges have those
            HRUs subtracted (so every HRU is touched by AT MOST one row).
        outlet_subbasin_id
            Which registered SWATPlusConfig holds the basin-wide TxtInOut
            template / binary / subbasin_attrs paths. Defaults to ``"bengbu"``.
        output_csv_paths
            Optional ``{subbasin_id: csv_path}`` mapping. If provided, a
            per-day ``date, Q_sim_m3s, Q_obs_m3s`` CSV is written for each
            worker as part of the single batched run.
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
                logger.error("No SWAT+ configs registered for batched run.")
                nan = _nan_metrics()
                return False, {sid: dict(nan) for sid in per_worker_params}
            base_cfg = next(iter(self._vic_configs.values()))

        from . import swatplus_runner as _sr
        worker_specs: List[_sr.WorkerSpec] = []
        for sid, cfg in self._vic_configs.items():
            if sid in per_worker_params:
                worker_specs.append(_sr.WorkerSpec(
                    subbasin_id=sid,
                    hru_fids=list(cfg.hru_fids),
                    obs_file=cfg.obs_file,
                    start_date=cfg.start_date,
                    end_date=cfg.end_date,
                ))

        tag = phase_tag or f"batched_r{run_id}"
        log = log_path or f"/tmp/swatplus_batched_{run_id}.log"

        ok, metrics_by_sid, sim_by_sid, obs_by_sid = _sr.run_once_batched(
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

        # Stash per-worker series for the graph node to consume.
        for sid in per_worker_params:
            sim = sim_by_sid.get(sid, {})
            obs = obs_by_sid.get(sid, {})
            self._last_series[sid] = (sim, obs)

        if not ok:
            nan = _nan_metrics()
            return False, {sid: dict(nan) for sid in per_worker_params}
        return True, metrics_by_sid

    def validate_all(self) -> Dict[str, List[str]]:
        """Validate all registered SWAT+ configs. Returns {basin: [missing_paths]}."""
        issues = {}
        for sid, cfg in self._vic_configs.items():
            missing = cfg.validate()
            if missing:
                issues[sid] = missing
        return issues


# Aliases preserved for any code that still imports the old names.
VICExecutor = SWATPlusExecutor
VICConfig = SWATPlusConfig
