#!/usr/bin/env python3
"""VIC model execution wrapper.

Provides a clean interface for running VIC+routing, compatible with
the local vic_runner module. Supports both real and simulate modes.
Per-station locks isolate execution and evidence retention.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Tuple

from .routing_utils import UpstreamRoutingInfo

from .execution_protocol import require_execution_inputs
from .native_audit import NativeAudit, json_safe
from .routing_cache import RoutingFluxCache, CacheMismatchError, _content_key

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
    eval_start_year: Optional[int] = None
    eval_end_year: Optional[int] = None
    flux_prefix: str = "fluxes_"
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
    runtime_event=None,
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
            eval_start_year=vic_config.eval_start_year,
            eval_end_year=vic_config.eval_end_year,
            runtime_event=runtime_event,
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
            metrics = {**_nan_metrics(), **metrics}
        return ok, metrics

    except Exception as e:
        logger.error("VIC execution failed for %s: %s", vic_config.basin_name, e)
        import traceback
        logger.error(traceback.format_exc())
        return False, {**_nan_metrics(), "native_error": type(e).__name__, "error_message": str(e)}


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
        self._audit = None
        self._cache = None
        self._reference_cache = None
        self.reference_phase = "VIC_ONLY"
        self._simulated_caches = {}
        self._basin_locks = {}
        self._configuration_lock = threading.RLock()

    def configure_runtime_audit(self, output_root, enabled=True, retain_candidates=True, **options):
        """Configure per-run evidence and restore its existing cache/accounting state."""
        with self._configuration_lock:
            root = Path(output_root).resolve()
            if self._audit is not None:
                if self._audit.root.parent != root:
                    raise ValueError("Executor audit root cannot change during a run")
                return self.get_accounting()
            self._audit = NativeAudit(root, enabled=enabled, retain_candidates=retain_candidates)
            self._cache = RoutingFluxCache(root) if self.mode == "real" else None
            if options.get("center_reference_cache", True) and self.mode == "real" and enabled and retain_candidates:
                from .center_reference_cache import CenterReferenceCache
                self._reference_cache = CenterReferenceCache(root)
        return self.get_accounting()

    def lookup_center_reference(self, subbasin_id, params, upstream_soil_info=None,
                                upstream_routing_info=None):
        """Return verified native evidence, without starting any evaluation."""
        if self._reference_cache is None:
            return None
        with self._basin_lock(subbasin_id):
            require_execution_inputs(params, upstream_routing_info=upstream_routing_info,
                                     upstream_soil_info=upstream_soil_info)
            key = self._reference_cache.signature(self, subbasin_id, params,
                                                   upstream_soil_info, upstream_routing_info)
            return self._reference_cache.lookup(key, subbasin_id, params)

    def reuse_center_reference(self, hit, destination, logical_round_id):
        self._reference_cache.materialize(hit, destination)
        record = hit["record"]
        self._audit.event({"attempt_id": record["attempt_id"], "kind": "center_reference_reuse",
                           "metadata": {"subbasin_id": record["subbasin_id"]}},
                          "center_reference_reused", logical_round_id=logical_round_id,
                          source_execution_identity={k: record[k] for k in ("run_id", "candidate_id", "phase_tag")},
                          cache_key=hit["cache_key"])

    def _ensure_audit(self, subbasin_id):
        if self._audit is None:
            cfg = self._vic_configs.get(subbasin_id)
            if cfg is not None:
                self.configure_runtime_audit(cfg.base_output_dir)
            else:
                # Simulate mode needs no output files. The baseline
                # graph calls configure_runtime_audit explicitly before this.
                self._audit = NativeAudit(Path.cwd(), enabled=False)
        return self._audit

    def get_accounting(self):
        if self._audit is None:
            counts = dict.fromkeys(NativeAudit.COUNTERS, 0)
        else:
            with self._audit.lock:
                counts = dict(self._audit.counts)
        return {**counts, "actual_routing_launches": counts["routing_launches"],
                "cache_preparation": counts["cache_preparation_attempts"],
                "mode": self.mode}

    def _basin_lock(self, sid):
        with self._configuration_lock:
            return self._basin_locks.setdefault(sid, threading.RLock())

    @staticmethod
    def _simulated_cache_key(params, upstream_soil_info):
        from .execution_protocol import SOIL_KEYS
        return _content_key({"soil": {k: params[k] for k in SOIL_KEYS},
                             "upstream": json_safe(upstream_soil_info or [])})

    def prepare_routing_cache(self, subbasin_id, params, upstream_soil_info=None,
                              upstream_routing_info=None):
        """Prepare/revalidate fixed VIC flux; routing is never launched here.

        Failure raises and never enables a failed cache. Explicit preparation
        may rebuild a stale cache; ordinary routing-only candidates may not.
        """
        audit = self._ensure_audit(subbasin_id)
        attempt = audit.start("cache_preparation", subbasin_id=subbasin_id,
                              parameters_submitted=params, mode=self.mode,
                              upstream_soil=json_safe(upstream_soil_info or []))
        try:
            with self._basin_lock(subbasin_id):
                require_execution_inputs(params, upstream_routing_info=upstream_routing_info,
                                         upstream_soil_info=upstream_soil_info)
                if self.mode == "simulate":
                    key = self._simulated_cache_key(params, upstream_soil_info)
                    reused = self._simulated_caches.get(subbasin_id) == key
                    self._simulated_caches[subbasin_id] = key
                    status = {"status": "reused_simulated" if reused else "prepared_simulated",
                              "subbasin_id": subbasin_id, "native_calls": 0, "key": key}
                else:
                    cfg = self._vic_configs.get(subbasin_id)
                    if cfg is None:
                        raise ValueError("No VIC config registered for " + subbasin_id)
                    status = self._cache.prepare(subbasin_id, cfg, params,
                                                 upstream_soil_info, audit.callback(attempt))
                audit.finish(attempt, True, cache_status=status, effective_parameters=params)
                return status
        except Exception as error:
            self._simulated_caches.pop(subbasin_id, None)
            if self._cache is not None:
                self._cache.deactivate(subbasin_id)
            audit.finish(attempt, False, error={"type": type(error).__name__, "message": str(error)})
            raise

    def clear_routing_cache(self, remove_flux=True):
        self._simulated_caches.clear()
        status = (self._cache.clear(remove_flux=remove_flux) if self._cache is not None
                  else {"status": "cleared_simulated", "basins": []})
        for cfg in self._vic_configs.values():
            cfg.vic_flux_source = ""
        return status

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
    ) -> Tuple[bool, Dict[str, float]]:
        """Evaluate a candidate, retaining small evidence and exact launch counts."""
        audit = self._ensure_audit(subbasin_id)
        attempt = audit.start("candidate", subbasin_id=subbasin_id, run_id=run_id,
                              candidate_id=candidate_id, phase_tag=phase_tag, mode=self.mode,
                              parameters_submitted=params,
                              upstream_soil=json_safe(upstream_soil_info or []),
                              upstream_routing=json_safe(upstream_routing_info or []))
        cache_status = {"status": "inactive"}
        valid_params = False
        try:
            with self._basin_lock(subbasin_id):
                require_execution_inputs(params, upstream_routing_info=upstream_routing_info,
                                         upstream_soil_info=upstream_soil_info)
                valid_params = True
                if self.mode == "simulate":
                    if subbasin_id in self._simulated_caches:
                        if self._simulated_caches[subbasin_id] != self._simulated_cache_key(params, upstream_soil_info):
                            raise CacheMismatchError("Simulated routing cache soil changed")
                        cache_status = {"status": "simulated_cache_hit"}
                        audit.event(attempt, "simulated_cache_hit")
                    ok, metrics = True, simulate_metrics(subbasin_id, run_id, candidate_id, params)
                else:
                    vic_cfg = self._vic_configs.get(subbasin_id)
                    if vic_cfg is None:
                        raise ValueError("No VIC config registered for basin: " + subbasin_id)
                    if subbasin_id in self._cache.active:
                        cache_status = self._cache.verify(subbasin_id, vic_cfg, params, upstream_soil_info)
                        vic_cfg = replace(vic_cfg, vic_flux_source=cache_status["flux_dir"])
                        audit.event(attempt, "cache_hit", cache_status=cache_status)
                    elif vic_cfg.vic_flux_source:
                        raise CacheMismatchError("Unmanifested vic_flux_source is not allowed")
                    tag = phase_tag or f"round_{run_id}_c{candidate_id}"
                    log = log_path or str(audit.root / "logs" / (attempt["attempt_id"] + ".log"))
                    input_key = (self._reference_cache.signature(self, subbasin_id, params,
                                 upstream_soil_info, upstream_routing_info) if self._reference_cache else None)
                    ok, metrics = run_vic_real(vic_cfg, run_id, params, tag, log,
                                              upstream_routing_info=upstream_routing_info,
                                              upstream_soil_info=upstream_soil_info,
                                              runtime_event=audit.callback(attempt))
                    if input_key and input_key != self._reference_cache.signature(
                            self, subbasin_id, params, upstream_soil_info, upstream_routing_info):
                        raise CacheMismatchError("Evaluation inputs changed during model execution")
                record = audit.finish(attempt, ok, metrics=metrics,
                             error=None if ok else {"type": "EvaluationFailure", "metrics": metrics},
                             effective_parameters=params, cache_status=cache_status,
                             evaluation_input_key=input_key if self.mode == "real" else None)
                if self._reference_cache is not None and input_key:
                    self._reference_cache.store(input_key, record)
                return ok, metrics
        except Exception as error:
            metrics = {**_nan_metrics(), "native_error" if valid_params else "parameter_error":
                       type(error).__name__ + ": " + str(error)}
            if not attempt.get("finished"):
                audit.finish(attempt, False, metrics=metrics,
                             error={"type": type(error).__name__, "message": str(error)},
                             effective_parameters=params if valid_params else None,
                             cache_status=cache_status)
            return False, metrics

    def validate_all(self) -> Dict[str, List[str]]:
        """Validate all registered VIC configs. Returns {basin: [missing_paths]}."""
        issues = {}
        for sid, cfg in self._vic_configs.items():
            missing = cfg.validate()
            if missing:
                issues[sid] = missing
        return issues
