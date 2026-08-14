"""
Initial Exploration Module — Pure code, zero LLM calls.

Before the agent-guided calibration loop begins, this module runs a
code-only parameter sweep to find a good starting center for each basin.

Strategy (per basin):
  - N_NEAR samples near the default center (Gaussian perturbation)
  - N_RANDOM samples uniformly across the full parameter range
  - All samples are evaluated with VIC+Routing
  - The best-performing parameter set becomes the new initial center

This addresses the high-variance cold-start problem observed in v1,
where the LLM-guided search sometimes started from a poor initial point.

Usage:
    from initial_explorer import run_initial_exploration
    results = run_initial_exploration(orchestrator)
"""

import logging
import math
import random
import time
from typing import Dict, List, Any, Optional, Tuple

import numpy as np

from .param_validator import normalize_params

logger = logging.getLogger(__name__)


def _generate_near_samples(
    center: Dict[str, float],
    param_ranges: Dict[str, Tuple[float, float]],
    active_keys: List[str],
    n_samples: int,
    spread: float = 0.15,
    log_params: Optional[List[str]] = None,
) -> List[Dict[str, float]]:
    """Generate samples near the default center with Gaussian perturbation.

    spread: fraction of parameter range used as std-dev.

    log_params (optional): keys sampled in LOG space (geometric perturbation
    about the center, std-dev = spread * log10(hi/lo) decades). Defaults to
    None -> no log keys, so existing (VIC/SWAT) callers are byte-identical.
    """
    log_set = set(log_params or [])
    samples = []
    for _ in range(n_samples):
        s = dict(center)
        for k in active_keys:
            if k not in param_ranges:
                continue
            lo, hi = param_ranges[k]
            if k in log_set and lo > 0 and hi > 0:
                c = center.get(k, math.sqrt(lo * hi))
                c = min(max(c, lo), hi)
                std_dec = spread * (math.log10(hi) - math.log10(lo))
                new_val = 10 ** (math.log10(c) + random.gauss(0, std_dec))
            else:
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
    log_params: Optional[List[str]] = None,
) -> List[Dict[str, float]]:
    """Generate Latin-Hypercube-like uniform random samples.

    For simplicity we use stratified random (divide each dim into n_samples
    strata) when n_samples <= 30, otherwise pure uniform.
    Non-active keys are kept at base_params values.

    log_params (optional): keys drawn LOG-uniformly across [lo, hi] (strata
    edges on log10 axis). Defaults to None -> no log keys, so existing
    (VIC/SWAT) callers are byte-identical.
    """
    log_set = set(log_params or [])

    def _is_log(k, lo, hi):
        return k in log_set and lo > 0 and hi > 0

    samples = []
    if n_samples <= 50:
        # Stratified random per dimension
        strata = {}
        for k in active_keys:
            if k not in param_ranges:
                continue
            lo, hi = param_ranges[k]
            if _is_log(k, lo, hi):
                edges = np.logspace(np.log10(lo), np.log10(hi), n_samples + 1)
                vals = [10 ** random.uniform(np.log10(edges[i]), np.log10(edges[i + 1]))
                        for i in range(n_samples)]
            else:
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
                if _is_log(k, lo, hi):
                    s[k] = 10 ** random.uniform(np.log10(lo), np.log10(hi))
                else:
                    s[k] = random.uniform(lo, hi)
            samples.append(s)
    return samples


def run_initial_exploration(
    orchestrator,
    n_near: int = 10,
    n_random: int = 20,
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

    # Run basin-by-basin in topological order (upstream first)
    # so downstream basins can use upstream best params for composite soil
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

            # Build upstream info (use current best from already-explored basins)
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
                nse = metrics.get("NSE", float("-inf")) if ok else float("-inf")
                basin_results.append({
                    "sample_id": i,
                    "params": params,
                    "metrics": metrics if ok else {},
                    "nse": nse,
                    "ok": ok,
                    "label": "default" if i == 0 else ("near" if i <= n_near else "random"),
                })

            # Sort by NSE descending
            basin_results.sort(key=lambda x: x["nse"], reverse=True)

            best = basin_results[0]
            top5_nse = [r["nse"] for r in basin_results[:5]]

            elapsed = time.time() - t_basin
            logger.info("  [%s] Done in %.0fs. Best NSE=%.4f (sample %d, %s). "
                        "Top-5 range: %.3f–%.3f",
                        sid, elapsed, best["nse"], best["sample_id"],
                        best["label"], min(top5_nse), max(top5_nse))

            # Build compact summary for Leader prompt (matches original TRA format)
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

            # Update latest_best_by_subbasin so downstream can use it
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


def format_exploration_summary(results: Dict[str, Dict]) -> str:
    """Format exploration results as a compact parameter-direction guide for Leader.

    Analyzes top-10 vs bottom-10 samples per basin to identify which param
    regions work well. ~200 tokens total — injected in Round 1.
    """
    lines = ["### Exploration Insights (50 samples/basin)"]
    for sid, r in results.items():
        all_results = r.get("all_results", [])
        if len(all_results) < 10:
            lines.append(f"{sid}: best NSE={r['best_nse']:.3f}")
            continue

        # Sort by NSE, compare top-10 vs bottom-10 param means
        sorted_r = sorted(all_results, key=lambda x: x.get("nse", -999), reverse=True)
        top = sorted_r[:10]
        bot = sorted_r[-10:]
        top_nses = [x["nse"] for x in top]
        hints = []
        for k in ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3"]:
            top_mean = np.mean([x["params"].get(k, 0) for x in top])
            bot_mean = np.mean([x["params"].get(k, 0) for x in bot])
            diff = top_mean - bot_mean
            if abs(diff) > 0.01:
                direction = "higher" if diff > 0 else "lower"
                hints.append(f"{k}={direction}")
        hint_str = ", ".join(hints) if hints else "no clear pattern"
        lines.append(f"{sid}: best={r['best_nse']:.3f}, top10 range={min(top_nses):.3f}-{max(top_nses):.3f}, good direction: {hint_str}")
    return "\n".join(lines)


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
