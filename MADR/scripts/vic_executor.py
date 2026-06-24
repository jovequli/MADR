#!/usr/bin/env python3
"""VIC model execution wrapper.

Provides a clean interface for running VIC+routing, compatible with
the local vic_runner module. Supports both real and simulate modes.
True parallel execution — no global state, no locks needed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .routing_utils import UpstreamRoutingInfo

logger = logging.getLogger(__name__)

PARAM_KEYS = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3", "velocity", "diffusivity"]


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Simulate mode: deterministic pseudo-metrics (no VIC execution)
# ---------------------------------------------------------------------------

def simulate_metrics(
    subbasin_id: str,
    round_id: int,
    candidate_id: int,
    params: Dict[str, float],
) -> Dict[str, float]:
    """Generate deterministic pseudo-metrics for testing the LLM agent flow."""
    seed = f"{subbasin_id}-{round_id}-{candidate_id}-{json.dumps(params, sort_keys=True)}"

    nse = 0.58
    nse += 0.08 * (1.0 - abs(params.get("binfilt", 0.3) - 0.24))
    nse += 0.06 * (1.0 - abs(params.get("Ws", 0.5) - 0.53))
    nse += 0.05 * (1.0 - abs(params.get("Ds", 0.1) - 0.10))
    nse += 0.05 * (1.0 - abs(params.get("Dsmax", 10.0) - 7.0) / 6.0)
    nse += 0.04 * (1.0 - abs(params.get("soil_d2", 0.5) - 0.40))
    nse += 0.04 * (1.0 - abs(params.get("soil_d3", 1.0) - 1.00) / 1.0)
    nse += _seeded_noise(seed + "-nse", -0.03, 0.03)
    nse = clamp(nse, -0.2, 0.95)

    rmse = 900 - 260 * nse + _seeded_noise(seed + "-rmse", -12, 12)
    pbias = _seeded_noise(seed + "-pbias", -14, 14)
    peak_ratio = _seeded_noise(seed + "-peak", 0.85, 1.15)
    lowflow_ratio = _seeded_noise(seed + "-lowf", 0.7, 1.5)

    kge = nse + _seeded_noise(seed + "-kge", -0.08, 0.05)
    kge = clamp(kge, -0.3, 0.95)

    return {
        "NSE": round(nse, 4),
        "KGE": round(kge, 4),
        "RMSE": round(rmse, 2),
        "PBIAS": round(pbias, 2),
        "peak_ratio": round(peak_ratio, 3),
        "lowflow_ratio": round(lowflow_ratio, 3),
    }


def _seeded_noise(key: str, lo: float, hi: float) -> float:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    u = int(h[:12], 16) / float(16**12 - 1)
    return lo + (hi - lo) * u


# ---------------------------------------------------------------------------
# Real mode: VIC + routing via local vic_runner (thread-safe)
# ---------------------------------------------------------------------------

@dataclass
class VICConfig:
    """Configuration for real VIC execution per sub-basin."""
    basin_name: str
    base_output_dir: str
    soil_base: str
    global_base: str
    obs_file: str
    rout_template_dir: str
    vic_exe: str = ""
    rout_exe: str = ""
    start_year: int = 1979
    end_year: int = 1980
    flux_prefix: str = "huaihe_fluxes_"
    vic_flux_source: str = ""  # Pre-existing flux dir for routing-only mode

    def validate(self) -> List[str]:
        """Check that required paths exist."""
        missing = []
        for attr in ("soil_base", "global_base", "obs_file", "rout_template_dir"):
            path = getattr(self, attr, "")
            if path and not os.path.exists(path):
                missing.append(f"{attr}: {path}")
        if self.vic_exe and not os.path.exists(self.vic_exe):
            missing.append(f"vic_exe: {self.vic_exe}")
        if self.rout_exe and not os.path.exists(self.rout_exe):
            missing.append(f"rout_exe: {self.rout_exe}")
        return missing


def run_vic_real(
    vic_config: VICConfig,
    run_id: int,
    params: Dict[str, float],
    phase_tag: str,
    log_path: str,
    upstream_routing_info: Optional[List[UpstreamRoutingInfo]] = None,
    upstream_soil_info: Optional[List["UpstreamSoilInfo"]] = None,
) -> Tuple[bool, Dict[str, float]]:
    """Run real VIC + routing model and return metrics.

    Uses local vic_runner module — fully thread-safe, no global state.
    """
    try:
        from . import vic_runner

        cfg = vic_runner.RuntimeConfig(
            basin_name=vic_config.basin_name,
            base_output_dir=vic_config.base_output_dir,
            soil_base=vic_config.soil_base,
            global_base=vic_config.global_base,
            obs_file=vic_config.obs_file,
            rout_template_dir=vic_config.rout_template_dir,
            vic_exe=vic_config.vic_exe,
            rout_exe=vic_config.rout_exe,
            start_year=int(vic_config.start_year),
            end_year=int(vic_config.end_year),
            flux_prefix=vic_config.flux_prefix,
        )
        routing_params = {
            "velocity": params.get("velocity", 1.5),
            "diffusivity": params.get("diffusivity", 800),
        }
        ok, metrics = vic_runner.run_once(
            cfg, run_id, params, phase_tag, log_path,
            routing_params=routing_params,
            upstream_routing_info=upstream_routing_info,
            upstream_soil_info=upstream_soil_info,
            vic_flux_source=vic_config.vic_flux_source,
        )

        if not ok:
            metrics = _nan_metrics()
        return ok, metrics

    except Exception as e:
        logger.error("VIC execution failed for %s: %s", vic_config.basin_name, e)
        import traceback
        logger.error(traceback.format_exc())
        return False, _nan_metrics()


def _nan_metrics() -> Dict[str, float]:
    return {
        "NSE": float("nan"),
        "KGE": float("nan"),
        "RMSE": float("nan"),
        "PBIAS": float("nan"),
        "peak_ratio": float("nan"),
        "lowflow_ratio": float("nan"),
    }


# ---------------------------------------------------------------------------
# Unified executor
# ---------------------------------------------------------------------------

class VICExecutor:
    """Unified VIC executor that dispatches to simulate or real mode."""

    def __init__(self, mode: str = "simulate"):
        if mode not in ("simulate", "real"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'simulate' or 'real'.")
        self.mode = mode
        self._vic_configs: Dict[str, VICConfig] = {}

    def register_basin(self, subbasin_id: str, vic_config: VICConfig):
        """Register a VIC configuration for a sub-basin (real mode only)."""
        self._vic_configs[subbasin_id] = vic_config

    def run(
        self,
        subbasin_id: str,
        run_id: int,
        candidate_id: int,
        params: Dict[str, float],
        phase_tag: str = "",
        log_path: str = "",
        upstream_routing_info: Optional[List[UpstreamRoutingInfo]] = None,
        upstream_soil_info: Optional[List["UpstreamSoilInfo"]] = None,
        upstream_cama_info: Optional[List[Any]] = None,
    ) -> Tuple[bool, Dict[str, float]]:
        """Execute VIC model (or simulation) for a parameter set.

        `upstream_cama_info` is accepted for a uniform executor interface but is
        unused here — CaMa channel locking only applies to vic_cama_flood.
        """
        if self.mode == "simulate":
            metrics = simulate_metrics(subbasin_id, run_id, candidate_id, params)
            return True, metrics
        else:
            vic_cfg = self._vic_configs.get(subbasin_id)
            if vic_cfg is None:
                logger.error("No VIC config registered for basin: %s", subbasin_id)
                return False, _nan_metrics()
            tag = phase_tag or f"round_{run_id}_c{candidate_id}"
            log = log_path or f"/tmp/vic_{subbasin_id}_{run_id}_{candidate_id}.log"
            return run_vic_real(vic_cfg, run_id, params, tag, log,
                                upstream_routing_info=upstream_routing_info,
                                upstream_soil_info=upstream_soil_info)

    def validate_all(self) -> Dict[str, List[str]]:
        """Validate all registered VIC configs. Returns {basin: [missing_paths]}."""
        issues = {}
        for sid, cfg in self._vic_configs.items():
            missing = cfg.validate()
            if missing:
                issues[sid] = missing
        return issues
