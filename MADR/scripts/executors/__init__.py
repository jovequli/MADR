"""Pluggable model-execution layer for MADR.cli.

A `ModelExecutor` runs one model combination's full per-candidate evaluation
pipeline: take parameters in, run the model(s), return metrics. Concrete
combinations live in sibling modules:

    vic_routing       -> VICRoutingExecutor      (VIC + Lohmann routing)
    vic_cama_flood    -> VICCamaFloodExecutor    (VIC + CaMa-Flood)

Call `make_executor(combination, ...)` to get the right one for the run.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


class ModelExecutor(ABC):
    """Abstract base for a model-combination executor.

    The contract is intentionally minimal — enough for graph_nodes.py to drive
    a calibration loop without knowing which models sit behind it.
    """

    # ------------------------------------------------------------------ run
    @abstractmethod
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
        """Run the model combination for one candidate.

        Returns (ok, metrics) where metrics is a dict with keys at least
        ``NSE``, ``KGE``, ``RMSE``, ``PBIAS``, ``peak_ratio``, ``lowflow_ratio``.
        Implementations may add more.
        """

    # --------------------------------------------------------- basin config
    @abstractmethod
    def register_basin(self, subbasin_id: str, basin_config: Any) -> None:
        """Register the per-basin configuration the executor needs."""

    @abstractmethod
    def validate_all(self) -> Dict[str, List[str]]:
        """Preflight check across all registered basins."""

    @abstractmethod
    def get_output_dir(self, subbasin_id: str) -> Optional[Path]:
        """Per-basin output directory. Return ``None`` if the executor
        does not produce a per-basin layout."""

    @abstractmethod
    def copy_routing_artifacts(
        self,
        subbasin_id: str,
        dst_dir: Path,
        candidate_tag: str = "",
    ) -> bool:
        """Copy this basin's routing/streamflow output from the last ``run()``
        call into ``dst_dir`` (creating ``dst_dir`` if needed). ``candidate_tag``
        (e.g. ``"c2_moderate"``) is appended to ``dst_dir`` for multi-candidate
        rounds so each candidate's output is preserved separately.

        Returns True if anything was copied. The executor knows its own output
        layout — graph_nodes just asks for the copy.
        """

    # -------------------------------- intermediate (land-surface) caching
    @abstractmethod
    def cache_intermediates_for_routing(
        self,
        centers: Dict[str, Dict[str, float]],
        output_root: Path,
        state: Dict[str, Any],
        worker_cfgs: List[Dict[str, Any]],
    ) -> None:
        """At the LAND_ONLY → ROUTING_ONLY transition, freeze the land-surface
        output (fluxes / NetCDF / whatever the routing model wants) so the
        routing-only rounds can skip re-running the land-surface model."""

    @abstractmethod
    def clear_intermediate_cache(self) -> None:
        """At ROUTING_ONLY → JOINT transition, drop the cache so JOINT runs
        the full pipeline again."""

    # --------------------------------------- per-phase tunable parameters
    @property
    @abstractmethod
    def tunable_keys_by_phase(self) -> Dict[str, Set[str]]:
        """Map ``phase_name -> set of tunable param keys`` for this combo.

        Example for vic_routing:
            {
              'VIC_ONLY':     {'binfilt','Ds','Dsmax','Ws','soil_d2','soil_d3'},
              'ROUTING_ONLY': {'velocity','diffusivity'},
              'JOINT':        {... all 8 ...},
            }
        """

    @property
    @abstractmethod
    def all_param_keys(self) -> List[str]:
        """Full list of parameters this combination calibrates."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_executor(combination: str, *, mode: str = "simulate", **kwargs) -> ModelExecutor:
    """Construct the right executor for the requested model combination.

    Args:
        combination: e.g. ``"vic_routing"``, ``"vic_cama_flood"``.
        mode: ``"simulate"`` (deterministic sim, no external binary) or
            ``"real"`` (run the actual model executables).
        **kwargs: forwarded to the concrete executor's ctor.

    Raises:
        ValueError: if ``combination`` is unknown.
    """
    name = (combination or "vic_routing").strip()
    if name == "vic_routing":
        from .vic_routing import VICRoutingExecutor
        return VICRoutingExecutor(mode=mode, **kwargs)
    if name == "vic_cama_flood":
        from .vic_cama_flood import VICCamaFloodExecutor
        return VICCamaFloodExecutor(mode=mode, **kwargs)
    if name == "swat":
        from .swat import SWATExecutor
        return SWATExecutor(mode=mode, **kwargs)
    if name == "wflow":
        from .wflow import WflowExecutor
        return WflowExecutor(mode=mode, **kwargs)
    raise ValueError(
        f"Unknown model_combination {name!r}. "
        f"Known: 'vic_routing', 'vic_cama_flood', 'swat', 'wflow'."
    )


__all__ = ["ModelExecutor", "make_executor"]
