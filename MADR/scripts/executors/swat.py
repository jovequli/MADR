"""SWAT+ executor (third combination, joining vic_routing and vic_cama_flood).

SWAT+ produces streamflow directly — there's no land-surface / routing split
like VIC has. We therefore expose a single calibration phase
``SWATPLUS_HRU`` with all 18 SWAT+ parameters tunable simultaneously.

The underlying ``SWATPlusExecutor`` (in ``scripts/swatplus_executor.py``)
already mirrors the legacy VIC executor's API (``run``, ``register_basin``,
``validate_all``). This file wraps it behind the modern ``ModelExecutor``
ABC so SWAT slots into the multi-model framework on equal footing with VIC
combinations.

There is no LAND -> ROUTING transition to cache for, so
``cache_intermediates_for_routing`` and ``clear_intermediate_cache`` are
no-ops. ``copy_routing_artifacts`` snapshots the per-run TxtInOut output
directory (the SWAT scratch dir is otherwise deleted after metrics are
computed).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..param_validator import SWAT_PARAM_KEYS
from ..swatplus_executor import SWATPlusExecutor, SWATPlusConfig
from . import ModelExecutor


class SWATExecutor(ModelExecutor):
    """SWAT+ executor exposed through the unified ModelExecutor surface."""

    def __init__(self, mode: str = "simulate", cleanup: bool = True):
        self._swat = SWATPlusExecutor(mode=mode, cleanup=cleanup)

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
        # SWAT+ ignores upstream_routing / upstream_soil — the model
        # internally handles its own routing and soil layering. Pass through
        # for API compatibility but discard inside SWATPlusExecutor.
        return self._swat.run(
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
        outlet_subbasin_id: str = "bengbu",
        output_csv_paths: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, Dict[str, Dict[str, float]]]:
        """ONE joint SWAT+ simulation applying every worker's params at once.

        Delegates to :meth:`SWATPlusExecutor.run_batched`, which writes a
        single calibration.cal with mutually exclusive HRU selectors per
        worker (nested HRU locking) in real mode, and computes per-site
        metrics from the combined param map in simulate mode (each site
        scored on its own params). Both modes share this call path; only
        the innermost execution differs.
        """
        return self._swat.run_batched(
            run_id=run_id,
            per_worker_params=per_worker_params,
            apply_order=apply_order,
            phase_tag=phase_tag,
            log_path=log_path,
            outlet_subbasin_id=outlet_subbasin_id,
            output_csv_paths=output_csv_paths,
        )

    def register_basin(self, subbasin_id: str, basin_config: Any) -> None:
        """Accepts a ``SWATPlusConfig`` (preferred) or a dict that we'll
        coerce into one for symmetry with the CaMa-Flood combo registration."""
        if isinstance(basin_config, SWATPlusConfig):
            self._swat.register_basin(subbasin_id, basin_config)
            return
        if isinstance(basin_config, dict):
            # Coerce dict -> SWATPlusConfig. The dict comes from the team
            # JSON's per-worker block and should already carry the SWAT
            # fields (txtinout_template, swatplus_exe, obs_file, etc.).
            cfg = SWATPlusConfig(**basin_config)
            self._swat.register_basin(subbasin_id, cfg)
            return
        raise TypeError(
            f"SWAT register_basin: expected SWATPlusConfig or dict, "
            f"got {type(basin_config).__name__}"
        )

    def validate_all(self) -> Dict[str, List[str]]:
        return self._swat.validate_all()

    def get_output_dir(self, subbasin_id: str) -> Optional[Path]:
        cfg = self._swat._vic_configs.get(subbasin_id)
        if cfg is None:
            return None
        out = getattr(cfg, "output_dir", None) or getattr(cfg, "txtinout_template", None)
        return Path(out) if out else None

    def copy_routing_artifacts(
        self,
        subbasin_id: str,
        dst_dir: Path,
        candidate_tag: str = "",
    ) -> bool:
        """SWAT+ runs in a per-candidate scratch dir; capture the simulated
        outlet hydrograph CSV (if produced) and the last log line. The full
        TxtInOut is intentionally NOT copied — it would be enormous over
        hundreds of rounds and SWAT itself can regenerate it from inputs."""
        cfg = self._swat._vic_configs.get(subbasin_id)
        if cfg is None:
            return False
        src = getattr(cfg, "last_run_dir", None)
        if not src or not Path(src).exists():
            return False
        src = Path(src)
        target = dst_dir / candidate_tag if candidate_tag else dst_dir
        target.mkdir(parents=True, exist_ok=True)
        copied = False
        for name in ("sim_outlet.csv", "channel_sd_day.csv"):
            p = src / name
            if p.is_file():
                shutil.copy2(str(p), str(target / name))
                copied = True
        return copied

    # ------------------------------ LAND->ROUTING caching (no-op for SWAT)

    def cache_intermediates_for_routing(
        self,
        centers: Dict[str, Dict[str, float]],
        output_root: Path,
        state: Dict[str, Any],
        worker_cfgs: List[Dict[str, Any]],
    ) -> None:
        # SWAT+ has no land/routing split. The single phase tunes everything
        # at once; this hook is structurally unused.
        return

    def clear_intermediate_cache(self) -> None:
        return

    # ----------------------------------- phase / param metadata

    @property
    def tunable_keys_by_phase(self) -> Dict[str, Set[str]]:
        # Single phase: every SWAT+ parameter is tunable.
        return {
            "SWATPLUS_HRU": set(SWAT_PARAM_KEYS),
            # JOINT alias kept for compatibility with any caller that
            # expects the canonical phase name set.
            "JOINT":        set(SWAT_PARAM_KEYS),
        }

    @property
    def all_param_keys(self) -> List[str]:
        return list(SWAT_PARAM_KEYS)
