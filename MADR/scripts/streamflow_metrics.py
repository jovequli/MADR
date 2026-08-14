#!/usr/bin/env python3
"""Shared streamflow performance metrics (model-agnostic).

Single source of truth for the daily-discharge metrics the VIC and SWAT+ runners
both need, so they stay consistent (e.g. one KGE definition). All functions take
aligned sim/obs LISTS (equal length, same ordered dates). ``compute_all_metrics``
returns the full dict the diagnostics module consumes. Math is the VIC reference
implementation, moved here verbatim.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Dict, List, Optional


def compute_ratio(sim: List[float], obs: List[float],
                  top_frac: float = 0.05, low: bool = False) -> float:
    if not sim or not obs:
        return float("nan")
    k = max(1, int(len(sim) * top_frac))
    pairs = sorted(zip(sim, obs), key=lambda x: x[1], reverse=not low)[:k]
    sim_mean = sum(p[0] for p in pairs) / len(pairs)
    obs_mean = sum(p[1] for p in pairs) / len(pairs)
    if obs_mean == 0:
        return float("nan")
    return sim_mean / obs_mean


def _find_peaks(values: List[float], threshold: float,
                min_distance: int = 7) -> List[int]:
    """Find local maxima above threshold, at least min_distance apart."""
    peaks = []
    n = len(values)
    for i in range(1, n - 1):
        if values[i] <= threshold:
            continue
        if values[i] >= values[i - 1] and values[i] >= values[i + 1]:
            if not peaks or (i - peaks[-1]) >= min_distance:
                peaks.append(i)
    return peaks


def _peak_timing_bias(sim: List[float], obs: List[float],
                      dates: Optional[List[dt.date]] = None,
                      match_window: int = 5) -> float:
    """Average peak timing bias in days. Negative = sim peaks early."""
    try:
        if len(obs) < 30:
            return float("nan")
        sorted_obs = sorted(obs)
        q90 = sorted_obs[int(len(sorted_obs) * 0.9)]
        obs_peaks = _find_peaks(obs, threshold=q90, min_distance=7)
        sim_peaks = _find_peaks(sim, threshold=q90, min_distance=7)
        if not obs_peaks or not sim_peaks:
            return float("nan")
        biases = []
        for op in obs_peaks:
            dists = [(abs(sp - op), sp) for sp in sim_peaks]
            min_abs, nearest_sp = min(dists, key=lambda x: x[0])
            if min_abs <= match_window:
                if dates is not None:
                    biases.append((dates[nearest_sp] - dates[op]).days)
                else:
                    biases.append(nearest_sp - op)
        return round(sum(biases) / len(biases), 1) if biases else float("nan")
    except Exception:
        return float("nan")


def _peak_magnitude_bias(sim: List[float], obs: List[float],
                         match_window: int = 5) -> float:
    """Percent bias in top-10 peak magnitudes."""
    try:
        if len(obs) < 30:
            return float("nan")
        sorted_obs = sorted(obs)
        q90 = sorted_obs[int(len(sorted_obs) * 0.9)]
        obs_peaks = _find_peaks(obs, threshold=q90, min_distance=7)
        sim_peaks = _find_peaks(sim, threshold=q90, min_distance=7)
        if not obs_peaks or not sim_peaks:
            return float("nan")
        biases = []
        for op in obs_peaks[:10]:
            dists = [(abs(sp - op), sp) for sp in sim_peaks]
            min_abs, nearest_sp = min(dists, key=lambda x: x[0])
            if min_abs <= match_window:
                obs_val = obs[op]
                sim_val = sim[nearest_sp]
                if obs_val > 0:
                    biases.append(100.0 * (sim_val - obs_val) / obs_val)
        return round(sum(biases) / len(biases), 1) if biases else float("nan")
    except Exception:
        return float("nan")


def _recession_k_ratio(sim: List[float], obs: List[float]) -> float:
    """Ratio of recession constants (sim/obs). >1 means sim recedes too fast."""
    try:
        if len(obs) < 30:
            return float("nan")
        min_length = 5

        def _fit_recession_k(flow: List[float]) -> float:
            segments_k = []
            i = 0
            n = len(flow)
            while i < n - min_length:
                if flow[i + 1] < flow[i] and flow[i] > 0:
                    seg_start = i
                    j = i + 1
                    while j < n and flow[j] < flow[j - 1] and flow[j] > 0:
                        j += 1
                    seg_len = j - seg_start
                    if seg_len >= min_length:
                        log_q = [math.log(flow[k]) for k in range(seg_start, j)
                                 if flow[k] > 0]
                        if len(log_q) >= min_length:
                            t = list(range(len(log_q)))
                            n_pts = len(t)
                            mean_t = sum(t) / n_pts
                            mean_lq = sum(log_q) / n_pts
                            num = sum((t[k] - mean_t) * (log_q[k] - mean_lq)
                                      for k in range(n_pts))
                            den = sum((t[k] - mean_t) ** 2 for k in range(n_pts))
                            if den > 0:
                                slope = abs(num / den)
                                segments_k.append(slope)
                    i = j
                else:
                    i += 1
            return sum(segments_k) / len(segments_k) if segments_k else float("nan")

        k_sim = _fit_recession_k(sim)
        k_obs = _fit_recession_k(obs)
        if math.isnan(k_sim) or math.isnan(k_obs) or k_obs == 0:
            return float("nan")
        return round(k_sim / k_obs, 2)
    except Exception:
        return float("nan")


def _baseflow_index_bias(sim: List[float], obs: List[float]) -> float:
    """Percent bias in baseflow index using Lyne-Hollick filter (alpha=0.925)."""
    try:
        if len(obs) < 30:
            return float("nan")
        alpha = 0.925

        def _bfi(flow: List[float]) -> float:
            qf = [0.0] * len(flow)
            qf[0] = flow[0]
            for i in range(1, len(flow)):
                qf[i] = alpha * qf[i - 1] + (1 + alpha) / 2.0 * (flow[i] - flow[i - 1])
                qf[i] = max(0.0, min(qf[i], flow[i]))
            baseflow = [flow[i] - qf[i] for i in range(len(flow))]
            total = sum(flow)
            if total == 0:
                return float("nan")
            return sum(baseflow) / total

        bfi_sim = _bfi(sim)
        bfi_obs = _bfi(obs)
        if math.isnan(bfi_sim) or math.isnan(bfi_obs) or bfi_obs == 0:
            return float("nan")
        return round(100.0 * (bfi_sim - bfi_obs) / bfi_obs, 1)
    except Exception:
        return float("nan")


def base_metrics(sim: List[float], obs: List[float]) -> Dict[str, float]:
    """NSE / KGE / PBIAS / RMSE / R2 / peak_ratio / lowflow_ratio from aligned lists.

    Reference math from vic_runner (standard KGE-2009: alpha = std_s/std_o).
    PBIAS convention: 100*sum(sim-obs)/sum(obs), positive = too wet. Raw (unrounded).
    """
    n = len(sim)
    if n == 0 or n != len(obs):
        return {"NSE": float("nan"), "KGE": float("nan"), "PBIAS": float("nan"),
                "RMSE": float("nan"), "R2": float("nan"),
                "peak_ratio": float("nan"), "lowflow_ratio": float("nan")}
    s, o = sim, obs
    mean_o = sum(o) / n
    mean_s = sum(s) / n
    num = sum((si - oi) ** 2 for si, oi in zip(s, o))
    den = sum((oi - mean_o) ** 2 for oi in o)
    nse = 1.0 - num / den if den > 0 else float("nan")
    rmse = math.sqrt(num / n)
    pbias = 100.0 * (sum(s) - sum(o)) / sum(o) if sum(o) != 0 else float("nan")

    cov_so = sum((si - mean_s) * (oi - mean_o) for si, oi in zip(s, o)) / n
    std_s = math.sqrt(sum((si - mean_s) ** 2 for si in s) / n)
    std_o = math.sqrt(sum((oi - mean_o) ** 2 for oi in o) / n)
    if std_o > 0 and std_s > 0:
        r = cov_so / (std_s * std_o)
        alpha = std_s / std_o
        beta = mean_s / mean_o if mean_o != 0 else float("nan")
        kge = (1.0 - math.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2)
               if beta == beta else float("nan"))
    else:
        r = float("nan")
        kge = float("nan")
    r2 = r * r if r == r else float("nan")

    peak_ratio = compute_ratio(s, o, top_frac=0.05)
    lowflow_ratio = compute_ratio(s, o, top_frac=0.20, low=True)
    return {
        "NSE": nse, "KGE": kge, "PBIAS": pbias, "RMSE": rmse, "R2": r2,
        "peak_ratio": peak_ratio, "lowflow_ratio": lowflow_ratio,
    }


def signatures(sim: List[float], obs: List[float],
               dates: Optional[List[dt.date]] = None,
               peak_match_window: int = 5) -> Dict[str, float]:
    """Hydrograph-shape signatures. NaN when the series is too short (<30)."""
    if len(obs) < 30:
        return {"peak_timing_bias_days": float("nan"),
                "peak_magnitude_bias_pct": float("nan"),
                "recession_k_ratio": float("nan"),
                "baseflow_index_bias_pct": float("nan")}
    return {
        "peak_timing_bias_days": _peak_timing_bias(sim, obs, dates=dates, match_window=peak_match_window),
        "peak_magnitude_bias_pct": _peak_magnitude_bias(sim, obs, match_window=peak_match_window),
        "recession_k_ratio": _recession_k_ratio(sim, obs),
        "baseflow_index_bias_pct": _baseflow_index_bias(sim, obs),
    }


def compute_all_metrics(sim: List[float], obs: List[float],
                        dates: Optional[List[dt.date]] = None,
                        peak_match_window: int = 5) -> Dict[str, float]:
    """Full metric set: base stats + hydrograph-shape signatures, in one dict."""
    m = base_metrics(sim, obs)
    m.update(signatures(sim, obs, dates, peak_match_window=peak_match_window))
    return m
