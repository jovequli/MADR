"""wflow_sbm executor (fourth combination, joining vic_routing, vic_cama_flood
and swat).

wflow_sbm produces streamflow directly through its integrated kinematic-wave
routing — there is no land-surface / routing split like VIC has. We therefore
expose a single calibration phase ``WFLOW_SBM`` with all wflow_sbm parameters
tunable simultaneously (mirroring SWAT+'s single ``SWATPLUS_HRU`` phase).

The underlying ``WflowExecutor`` (in ``scripts/wflow_executor.py``) already
mirrors the SWAT+ / legacy VIC executor's API (``run``, ``register_basin``,
``run_batched``, ``validate_all``). This file wraps it behind the modern
``ModelExecutor`` ABC so wflow slots into the multi-model framework on equal
footing with VIC and SWAT.

There is no LAND -> ROUTING transition to cache for, so
``cache_intermediates_for_routing`` and ``clear_intermediate_cache`` are
no-ops (identical to the SWAT executor).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..param_validator import WFLOW_PARAM_KEYS
from ..wflow_executor import WflowExecutor as _WflowRunExecutor, WflowConfig
from . import ModelExecutor


class WflowExecutor(ModelExecutor):
    """wflow_sbm executor exposed through the unified ModelExecutor surface."""

    def __init__(self, mode: str = "simulate", cleanup: bool = True):
        self._wflow = _WflowRunExecutor(mode=mode, cleanup=cleanup)

    # ---------------- ModelExecutor surface ----------------

    def run(
        self,
        subbasin_id: str,
        run_id: int,
        candidate_id: int,
        params: Dict[str, float],
        phase_tag: str = "",
        log_path: str = "",
        upstream_routing_info: Optional[List[Any]] = None,
        upstream_soil_info: Optional[List[Any]] = None,
    ) -> Tuple[bool, Dict[str, float]]:
        # wflow_sbm ignores upstream_routing / upstream_soil — it internally
        # handles its own routing and soil layering. Pass through for API
        # compatibility but discard inside WflowExecutor.
        return self._wflow.run(
            subbasin_id=subbasin_id,
            run_id=run_id,
            candidate_id=candidate_id,
            params=params,
            phase_tag=phase_tag,
            log_path=log_path,
            upstream_routing_info=upstream_routing_info,
            upstream_soil_info=upstream_soil_info,
        )

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
        """ONE joint wflow_sbm simulation applying every worker's params at once.

        Delegates to :meth:`WflowExecutor.run_batched`, which writes a single
        per-run staticmaps.nc with mutually exclusive cell selectors per worker
        (nested cell locking) in real mode, and computes per-site metrics from
        the combined param map in simulate mode.
        """
        return self._wflow.run_batched(
            run_id=run_id,
            per_worker_params=per_worker_params,
            apply_order=apply_order,
            phase_tag=phase_tag,
            log_path=log_path,
            outlet_subbasin_id=outlet_subbasin_id,
            output_csv_paths=output_csv_paths,
        )

    def register_basin(self, subbasin_id: str, basin_config: Any) -> None:
        """Accepts a ``WflowConfig`` (preferred) or a dict coerced into one."""
        if isinstance(basin_config, WflowConfig):
            self._wflow.register_basin(subbasin_id, basin_config)
            return
        if isinstance(basin_config, dict):
            cfg = WflowConfig(**basin_config)
            self._wflow.register_basin(subbasin_id, cfg)
            return
        raise TypeError(
            f"wflow register_basin: expected WflowConfig or dict, "
            f"got {type(basin_config).__name__}"
        )

    def validate_all(self) -> Dict[str, List[str]]:
        return self._wflow.validate_all()

    def get_output_dir(self, subbasin_id: str) -> Optional[Path]:
        cfg = self._wflow._vic_configs.get(subbasin_id)
        if cfg is None:
            return None
        out = getattr(cfg, "output_dir", None) or getattr(cfg, "wflow_model_dir", None)
        return Path(out) if out else None

    def copy_routing_artifacts(
        self,
        subbasin_id: str,
        dst_dir: Path,
        candidate_tag: str = "",
    ) -> bool:
        """wflow runs in a per-candidate scratch dir that is deleted after
        metrics are computed; there is no persistent per-run artifact to copy
        by default (the discharge CSV is written by the graph node from the
        cached series). Return False, matching the lightweight SWAT behaviour
        when no last_run_dir is retained."""
        cfg = self._wflow._vic_configs.get(subbasin_id)
        if cfg is None:
            return False
        src = getattr(cfg, "last_run_dir", None)
        if not src or not Path(src).exists():
            return False
        src = Path(src)
        target = dst_dir / candidate_tag if candidate_tag else dst_dir
        target.mkdir(parents=True, exist_ok=True)
        copied = False
        for name in ("sim_outlet.csv", "gauge_q.csv"):
            p = src / name
            if p.is_file():
                shutil.copy2(str(p), str(target / name))
                copied = True
        return copied

    # ------------------------------ LAND->ROUTING caching (no-op for wflow)

    def cache_intermediates_for_routing(
        self,
        centers: Dict[str, Dict[str, float]],
        output_root: Path,
        state: Dict[str, Any],
        worker_cfgs: List[Dict[str, Any]],
    ) -> None:
        # wflow_sbm has no land/routing split. The single phase tunes
        # everything at once; this hook is structurally unused.
        return

    def clear_intermediate_cache(self) -> None:
        return

    # ----------------------------------- phase / param metadata

    @property
    def tunable_keys_by_phase(self) -> Dict[str, Set[str]]:
        # Single phase: every wflow_sbm parameter is tunable.
        return {
            "WFLOW_SBM": set(WFLOW_PARAM_KEYS),
            # JOINT alias kept for compatibility with any caller that expects
            # the canonical phase name set.
            "JOINT":     set(WFLOW_PARAM_KEYS),
        }

    @property
    def all_param_keys(self) -> List[str]:
        return list(WFLOW_PARAM_KEYS)
