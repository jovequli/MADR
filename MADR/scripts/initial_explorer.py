"""Numerical exploration without LLM calls.

Evaluate near-default and full-range samples, then summarize their outcomes for
calibration. Exploration does not replace the configured initial centers or
enter the calibration ledger; the first calibration round keeps its defaults."""

import logging
import random
import time
from typing import Dict, List, Any, Optional, Tuple

import numpy as np

from .param_validator import normalize_params
from .execution_protocol import candidate_score, is_finite

logger = logging.getLogger(__name__)


def _generate_near_samples(
    center: Dict[str, float],
    param_ranges: Dict[str, Tuple[float, float]],
    active_keys: List[str],
    n_samples: int,
    spread: float = 0.15,
) -> List[Dict[str, float]]:
    """Generate samples near the default center with Gaussian perturbation.

    spread: fraction of parameter range used as std-dev.
    """
    samples = []
    for _ in range(n_samples):
        s = dict(center)
        for k in active_keys:
            if k not in param_ranges:
                continue
            lo, hi = param_ranges[k]
            std = (hi - lo) * spread
            new_val = center[k] + random.gauss(0, std)
            s[k] = max(lo, min(hi, new_val))
        samples.append(s)
    return samples


def _generate_random_samples(
    param_ranges: Dict[str, Tuple[float, float]],
    active_keys: List[str],
    base_params: Dict[str, float],
    n_samples: int,
) -> List[Dict[str, float]]:
    """Generate Latin-Hypercube-like uniform random samples.

    For simplicity we use stratified random (divide each dim into n_samples
    strata) when n_samples <= 50, otherwise pure uniform.
    Non-active keys are kept at base_params values.
    """
    samples = []
    if n_samples <= 50:
        # Stratified random per dimension
        strata = {}
        for k in active_keys:
            if k not in param_ranges:
                continue
            lo, hi = param_ranges[k]
            edges = np.linspace(lo, hi, n_samples + 1)
            vals = [random.uniform(edges[i], edges[i + 1]) for i in range(n_samples)]
            random.shuffle(vals)
            strata[k] = vals

        for i in range(n_samples):
            s = dict(base_params)
            for k in active_keys:
                if k in strata:
                    s[k] = strata[k][i]
            samples.append(s)
    else:
        for _ in range(n_samples):
            s = dict(base_params)
            for k in active_keys:
                if k not in param_ranges:
                    continue
                lo, hi = param_ranges[k]
                s[k] = random.uniform(lo, hi)
            samples.append(s)
    return samples


def run_initial_exploration(
    orchestrator,
    n_near: int = 10,
    n_random: int = 40,
    vic_only_keys: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run initial exploration for all basins.

    Parameters
    ----------
    orchestrator : Orchestrator
        The main orchestrator instance (provides vic_executor, centers,
        param_ranges, topo_levels, workers, etc.)
    n_near : int
        Number of near-center samples per basin.
    n_random : int
        Number of full-range random samples per basin.
    vic_only_keys : list, optional
        Active parameter keys. Defaults to VIC soil params only.

    Returns
    -------
    dict
        {basin_id: {"best_params": {...}, "best_nse": float,
                     "all_results": [...], "summary_text": str}}
    """
    if vic_only_keys is None:
        vic_only_keys = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3"]

    n_total = n_near + n_random
    logger.info("="*60)
    logger.info("INITIAL EXPLORATION: %d samples/basin (%d near + %d random)",
                n_total, n_near, n_random)
    logger.info("="*60)

    results = {}
    t_start = time.time()

    # Evaluate basins in topological order using configured upstream centers.
    # Exploration results do not replace those centers.
    for level_sids in orchestrator.topo_levels:
        for sid in level_sids:
            t_basin = time.time()
            center = orchestrator.centers[sid]
            param_ranges = orchestrator.param_ranges

            # Generate samples
            near_samples = _generate_near_samples(
                center, param_ranges, vic_only_keys, n_near
            )
            random_samples = _generate_random_samples(
                param_ranges, vic_only_keys, center, n_random
            )
            # R1 will run with exact defaults, so exploration skips it
            all_samples = near_samples + random_samples
            # Enforce physical constraints (e.g., soil_d3 > soil_d2)
            all_samples = [normalize_params(s, param_ranges) for s in all_samples]

            logger.info("  [%s] Evaluating %d samples (%d near + %d random)...",
                        sid, len(all_samples), n_near, n_random)

            # Read upstream parameters from the unchanged configured centers;
            # exploration optima do not replace the downstream input background.
            up_routing_info = orchestrator._build_upstream_routing_info(sid)
            up_soil_info = orchestrator._build_upstream_soil_info(sid)

            # Evaluate all samples
            basin_results = []
            for i, params in enumerate(all_samples):
                ok, metrics = orchestrator.vic_executor.run(
                    subbasin_id=sid,
                    run_id=i + 1,
                    candidate_id=i + 1,
                    params=params,
                    phase_tag=f"init_explore_{i+1:03d}",
                    upstream_routing_info=up_routing_info,
                    upstream_soil_info=up_soil_info,
                )
                nse = candidate_score(ok, metrics)
                basin_results.append({
                    "sample_id": i,
                    "params": params,
                    "metrics": metrics if ok else {},
                    "nse": nse,
                    "ok": ok,
                    "label": "near" if i < n_near else "random",
                })

            # Sort by NSE descending
            basin_results.sort(key=lambda x: x["nse"], reverse=True)

            best = basin_results[0]
            if not is_finite(best["nse"]):
                raise RuntimeError("All initial exploration candidates invalid: " + sid)
            top5_nse = [r["nse"] for r in basin_results[:5]]

            elapsed = time.time() - t_basin
            logger.info("  [%s] Done in %.0fs. Best NSE=%.4f (sample %d, %s). "
                        "Top-5 range: %.3f–%.3f",
                        sid, elapsed, best["nse"], best["sample_id"],
                        best["label"], min(top5_nse), max(top5_nse))

            # Build compact summary for Leader prompt
            summary = (
                f"{sid}: best_NSE={best['nse']:.4f} ({best['label']}) | "
                f"top5=[{', '.join(f'{n:.3f}' for n in top5_nse)}] | "
                f"params={_compact_params(best['params'], vic_only_keys)}"
            )

            results[sid] = {
                "best_params": best["params"],
                "best_nse": best["nse"],
                "best_metrics": best["metrics"],
                "all_results": basin_results,
                "n_samples": len(all_samples),
                "summary_text": summary,
            }

            # Exploration is informational only — do NOT update centers
            # Centers stay at expert defaults; calibration updates them dynamically

            # Retain the exploration optimum as a Leader memory record only;
            # downstream model inputs still use the unchanged centers.
            prev = orchestrator.leader.memory.latest_best_by_subbasin.get(sid, {})
            prev_nse = prev.get("metrics", {}).get("NSE", float("-inf"))
            if best["nse"] > prev_nse:
                orchestrator.leader.memory.latest_best_by_subbasin[sid] = {
                    "round_id": 0,
                    "params": best["params"],
                    "metrics": best["metrics"],
                }

            # Do NOT write exploration best to worker ledger.
            # Ledger tracks calibration rounds only, so center updates
            # compare against calibration history (not exploration best).

    total_elapsed = time.time() - t_start
    total_sims = sum(r["n_samples"] for r in results.values())
    logger.info("="*60)
    logger.info("INITIAL EXPLORATION COMPLETE: %d sims in %.0fs", total_sims, total_elapsed)
    for sid, r in results.items():
        logger.info("  %s", r["summary_text"])
    logger.info("="*60)

    return results


def format_exploration_summary(results):
    from .diagnostic_evidence import exploration_statistics
    return exploration_statistics(results)


def _compact_params(params: Dict[str, float], keys: List[str]) -> str:
    """Format params as compact string."""
    parts = []
    for k in keys:
        if k in params:
            v = params[k]
            if abs(v) >= 100:
                parts.append(f"{k}={v:.1f}")
            elif abs(v) >= 1:
                parts.append(f"{k}={v:.3f}")
            else:
                parts.append(f"{k}={v:.4f}")
    return "{" + ", ".join(parts) + "}"
