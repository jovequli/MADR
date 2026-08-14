"""VIC + Lohmann routing executor.

This is the original MADR.cli pipeline wrapped behind the ``ModelExecutor`` ABC.
It delegates straight to ``scripts/vic_executor.py`` for the actual VIC and
routing calls — no behavioural change versus pre-refactor MADR.cli.

The intermediate-flux caching that used to live in
``graph_nodes._cache_vic_fluxes_for_routing`` moves here so it stays close
to the executor that owns the cache layout.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..param_validator import (
    SOIL_PARAM_KEYS,
    ROUTING_PARAM_KEYS,
    PARAM_KEYS,
)
from ..vic_executor import VICExecutor, VICConfig
from . import ModelExecutor


class VICRoutingExecutor(ModelExecutor):
    """VIC + Lohmann routing: today's MADR.cli behaviour as a swappable unit."""

    def __init__(self, mode: str = "simulate"):
        self._vic = VICExecutor(mode=mode)

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
        return self._vic.run(
            subbasin_id=subbasin_id,
            run_id=run_id,
            candidate_id=candidate_id,
            params=params,
            phase_tag=phase_tag,
            log_path=log_path,
            upstream_routing_info=upstream_routing_info,
            upstream_soil_info=upstream_soil_info,
        )

    def register_basin(self, subbasin_id: str, basin_config: VICConfig) -> None:
        self._vic.register_basin(subbasin_id, basin_config)

    def validate_all(self) -> Dict[str, List[str]]:
        return self._vic.validate_all()

    def get_output_dir(self, subbasin_id: str) -> Optional[Path]:
        vic_cfg = self._vic._vic_configs.get(subbasin_id)
        if vic_cfg is None:
            return None
        return Path(vic_cfg.base_output_dir)

    def copy_routing_artifacts(
        self,
        subbasin_id: str,
        dst_dir: Path,
        candidate_tag: str = "",
    ) -> bool:
        vic_cfg = self._vic._vic_configs.get(subbasin_id)
        if vic_cfg is None:
            return False
        src = (
            Path(vic_cfg.base_output_dir)
            / f"{subbasin_id}_calibration_ai"
            / "current_run"
            / "routing_result"
        )
        if not src.exists():
            return False
        target = dst_dir / candidate_tag if candidate_tag else dst_dir
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src), str(target))
        return True

    def cache_intermediates_for_routing(
        self,
        centers: Dict[str, Dict[str, float]],
        output_root: Path,
        state: Dict[str, Any],
        worker_cfgs: List[Dict[str, Any]],
    ) -> None:
        """Run VIC once per basin at its best soil params, snapshot the
        post-processed flux directory, and point the basin's VICConfig at it
        so ROUTING_ONLY rounds skip VIC.

        Imports of the upstream-info builders are deferred to avoid an import
        cycle (graph_nodes imports executors, executors import from graph_nodes).
        """
        from ..graph_nodes import (
            _build_upstream_routing_info,
            _build_upstream_soil_info,
        )

        cache_root = output_root / "cached_vic_runoff"
        cache_root.mkdir(parents=True, exist_ok=True)
        print("  >>> Caching VIC fluxes for ROUTING_ONLY phase...")

        for w_cfg in worker_cfgs:
            sid = w_cfg["subbasin_id"]
            vic_cfg = self._vic._vic_configs.get(sid)
            if vic_cfg is None:
                print(f"      {sid}: no VIC config, skipping cache")
                continue

            best_params = dict(centers.get(sid, {}))
            if not best_params:
                print(f"      {sid}: no best params, skipping cache")
                continue

            up_routing = _build_upstream_routing_info(sid, state, worker_cfgs)
            up_soil = _build_upstream_soil_info(sid, state, worker_cfgs)
            ok, _ = self._vic.run(
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

            src = (
                Path(vic_cfg.base_output_dir)
                / f"{sid}_calibration_ai"
                / "current_run"
                / "vic_for_routing"
            )
            if not src.exists():
                print(f"      {sid}: vic_for_routing not found at {src}")
                continue

            dst = cache_root / sid
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(str(src), str(dst))
            vic_cfg.vic_flux_source = str(dst)
            print(f"      {sid}: cached → {dst}")

    def clear_intermediate_cache(self) -> None:
        for sid, vic_cfg in self._vic._vic_configs.items():
            vic_cfg.vic_flux_source = ""
        print("  >>> Cleared VIC flux cache for JOINT phase")

    # ---------------- phase / param metadata ----------------

    @property
    def tunable_keys_by_phase(self) -> Dict[str, Set[str]]:
        return {
            "VIC_ONLY": set(SOIL_PARAM_KEYS),
            "ROUTING_ONLY": set(ROUTING_PARAM_KEYS),
            "JOINT": set(PARAM_KEYS),
        }

    @property
    def all_param_keys(self) -> List[str]:
        return list(PARAM_KEYS)

    # ---------------- back-compat helpers ----------------

    @property
    def vic_executor(self) -> VICExecutor:
        """Expose the underlying VICExecutor for legacy access sites
        (e.g. routing-result copy paths that look at ``_vic_configs``).
        Use sparingly — new code should call the abstract methods."""
        return self._vic
