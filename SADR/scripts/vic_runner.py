#!/usr/bin/env python3
"""Thread-safe VIC + routing execution module.

Refactored from skills/vic_cali_ai/run_bengbu_ai_calibration.py to eliminate
global variables.  Every function receives paths via a RuntimeConfig dataclass,
making concurrent execution across sub-basins safe.
"""

import csv
import datetime as dt
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import logging

from .routing_utils import (
    UpstreamRoutingInfo,
    find_direc_in_template,
    generate_composite_grid,
    generate_uniform_grid,
)

logger = logging.getLogger(__name__)

# ── constant lookup tables (read-only, thread-safe) ──────────────────────

INDEX_MAP = {
    "binfilt": 4,
    "Ds": 5,
    "Dsmax": 6,
    "Ws": 7,
    "soil_d2": 23,
    "soil_d3": 24,
}


@dataclass
class UpstreamSoilInfo:
    """Upstream basin's soil parameter information for composite soil writing."""
    soil_base: str              # Path to upstream basin's soil_base file
    params: Dict[str, float]    # Upstream basin's historical best VIC soil params


@dataclass
class RuntimeConfig:
    basin_name: str
    base_output_dir: str
    soil_base: str
    global_base: str
    obs_file: str
    rout_template_dir: str
    vic_exe: str
    rout_exe: str
    start_year: int
    end_year: int
    flux_prefix: str = "huaihe_fluxes_"

    @property
    def output_root(self) -> str:
        return os.path.join(self.base_output_dir, f"{self.basin_name}_calibration_ai")

    @property
    def current_dir(self) -> str:
        return os.path.join(self.output_root, "current_run")


# ── helpers ──────────────────────────────────────────────────────────────

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def reset_dir(path: str) -> None:
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


# ── observation / simulation I/O ─────────────────────────────────────────

def read_obs(obs_path: str, start: dt.date, end: dt.date) -> Dict[dt.date, float]:
    """Read observed discharge. Accepts both daily (`YYYY-M-D`) and monthly (`YYYY-M`)
    date strings. Monthly observations are keyed by `date(Y, M, 1)` so downstream
    code can detect monthly mode by checking whether all `.day` values equal 1.
    """
    obs: Dict[dt.date, float] = {}
    with open(obs_path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            try:
                date_str = row.get("dates") or row.get("date")
                if not date_str:
                    continue
                parts = date_str.split("-")
                if len(parts) == 3:
                    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                elif len(parts) == 2:
                    y, m, d = int(parts[0]), int(parts[1]), 1
                else:
                    continue
                date = dt.date(y, m, d)
                if date < start or date > end:
                    continue
                q = float(row.get("Q"))
                if q < 0:
                    continue
                obs[date] = q
            except Exception:
                continue
    return obs


def aggregate_sim_to_monthly(sim_daily: Dict[dt.date, float]) -> Dict[dt.date, float]:
    """Aggregate a daily-Q dict to monthly means keyed by `date(Y, M, 1)`."""
    bucket: Dict[dt.date, List[float]] = {}
    for d, v in sim_daily.items():
        if math.isnan(v) or math.isinf(v):
            continue
        key = dt.date(d.year, d.month, 1)
        bucket.setdefault(key, []).append(v)
    return {k: (sum(vs) / len(vs)) for k, vs in bucket.items() if vs}


def is_monthly_obs(obs: Dict[dt.date, float]) -> bool:
    """True iff every observation date falls on day=1 (interpreted as monthly)."""
    if not obs:
        return False
    return all(d.day == 1 for d in obs.keys())


def read_sim_day(sim_path: str, start: dt.date, end: dt.date) -> Dict[dt.date, float]:
    sim: Dict[dt.date, float] = {}
    with open(sim_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 4:
                continue
            try:
                y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                val = float(parts[3])
                date = dt.date(y, m, d)
            except Exception:
                continue
            if start <= date <= end:
                sim[date] = val
    return sim


# ── metrics ──────────────────────────────────────────────────────────────

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


def compute_metrics(obs: Dict[dt.date, float],
                    sim: Dict[dt.date, float]) -> Tuple[float, float, float, float, float, float]:
    dates = sorted(set(obs) & set(sim))
    if not dates:
        return (float("nan"),) * 6

    # 剔除缺测：obs < 0 表示缺测，同时排除 NaN / Inf
    o, s = [], []
    for d in dates:
        ov, sv = obs[d], sim[d]
        if ov < 0 or math.isnan(ov) or math.isinf(ov):
            continue
        if math.isnan(sv) or math.isinf(sv):
            continue
        o.append(ov)
        s.append(sv)
    if len(o) < 10:
        return (float("nan"),) * 6
    n = len(o)

    mean_o = sum(o) / n
    mean_s = sum(s) / n
    num = sum((si - oi) ** 2 for si, oi in zip(s, o))
    den = sum((oi - mean_o) ** 2 for oi in o)
    nse = 1.0 - num / den if den > 0 else float("nan")
    rmse = math.sqrt(num / n)
    pbias = 100.0 * (sum(s) - sum(o)) / sum(o) if sum(o) != 0 else float("nan")

    # KGE = 1 - sqrt((r-1)^2 + (alpha-1)^2 + (beta-1)^2)
    cov_so = sum((si - mean_s) * (oi - mean_o) for si, oi in zip(s, o)) / n
    std_s = math.sqrt(sum((si - mean_s) ** 2 for si in s) / n)
    std_o = math.sqrt(sum((oi - mean_o) ** 2 for oi in o) / n)
    if std_o > 0 and std_s > 0:
        r = cov_so / (std_s * std_o)
        alpha = std_s / std_o
        beta = mean_s / mean_o if mean_o != 0 else float("nan")
        if beta == beta:  # not NaN
            kge = 1.0 - math.sqrt((r - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2)
        else:
            kge = float("nan")
    else:
        kge = float("nan")

    peak_ratio = compute_ratio(s, o, top_frac=0.05)
    low_ratio = compute_ratio(s, o, top_frac=0.20, low=True)
    return nse, rmse, pbias, kge, peak_ratio, low_ratio


# ── extended hydrologic signatures ───────────────────────────────────────

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


def _peak_timing_bias(sim: List[float], obs: List[float]) -> float:
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
            dists = [(abs(sp - op), sp - op) for sp in sim_peaks]
            min_abs, bias = min(dists, key=lambda x: x[0])
            if min_abs <= 5:
                biases.append(bias)
        return round(sum(biases) / len(biases), 1) if biases else float("nan")
    except Exception:
        return float("nan")


def _peak_magnitude_bias(sim: List[float], obs: List[float]) -> float:
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
            if min_abs <= 5:
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
            """Find recession segments and fit log-linear slope."""
            segments_k = []
            i = 0
            n = len(flow)
            while i < n - min_length:
                # Find start of recession (flow decreasing)
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


def compute_signatures(obs_dict: Dict[dt.date, float],
                       sim_dict: Dict[dt.date, float]) -> Dict[str, float]:
    """Compute extended hydrologic signatures from obs/sim dicts."""
    dates = sorted(set(obs_dict) & set(sim_dict))
    if len(dates) < 30:
        return {
            "peak_timing_bias_days": float("nan"),
            "peak_magnitude_bias_pct": float("nan"),
            "recession_k_ratio": float("nan"),
            "baseflow_index_bias_pct": float("nan"),
        }
    o = [obs_dict[d] for d in dates]
    s = [sim_dict[d] for d in dates]
    return {
        "peak_timing_bias_days": _peak_timing_bias(s, o),
        "peak_magnitude_bias_pct": _peak_magnitude_bias(s, o),
        "recession_k_ratio": _recession_k_ratio(s, o),
        "baseflow_index_bias_pct": _baseflow_index_bias(s, o),
    }


# ── soil / global param writers ──────────────────────────────────────────

def _build_upstream_soil_mask(
    upstream_soil_infos: List[UpstreamSoilInfo],
) -> Dict[Tuple[int, int], Dict[str, float]]:
    """Build a (lat_key, lon_key) → params mask from upstream soil files.

    Upstream infos are expected in order: downstream-level first, upstream-level
    last (last-wins), so that the most-upstream basin's params take precedence.
    """
    mask: Dict[Tuple[int, int], Dict[str, float]] = {}
    for info in upstream_soil_infos:
        if not info.soil_base or not os.path.isfile(info.soil_base):
            logger.warning("Upstream soil_base not found: %s", info.soil_base)
            continue
        with open(info.soil_base, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 25:
                    continue
                try:
                    lat_key = round(float(parts[2]) * 1000)
                    lon_key = round(float(parts[3]) * 1000)
                except (ValueError, IndexError):
                    continue
                mask[(lat_key, lon_key)] = info.params
    return mask


def write_soil_params(
    src_path: str,
    dst_path: str,
    params: Dict[str, float],
    upstream_soil_infos: Optional[List[UpstreamSoilInfo]] = None,
) -> None:
    """Write soil parameter file with optional per-grid composite writing.

    If *upstream_soil_infos* is provided and non-empty, grid cells that belong
    to upstream basins will use their respective historical-best params, while
    this basin's own (unique) grid cells use *params*.  When no upstream info
    is given, all cells are written with *params* (backward-compatible).
    """
    # Build upstream mask if applicable
    upstream_mask: Dict[Tuple[int, int], Dict[str, float]] = {}
    if upstream_soil_infos:
        upstream_mask = _build_upstream_soil_mask(upstream_soil_infos)

    n_upstream = 0
    n_own = 0
    with open(src_path, "r", encoding="utf-8", errors="ignore") as f_in, \
         open(dst_path, "w", encoding="utf-8") as f_out:
        for line in f_in:
            parts = line.strip().split()
            if not parts or len(parts) < 25:
                f_out.write(line)
                continue

            # Determine which params to use for this grid cell
            cell_params = params  # default: own params
            if upstream_mask:
                try:
                    lat_key = round(float(parts[2]) * 1000)
                    lon_key = round(float(parts[3]) * 1000)
                    up_params = upstream_mask.get((lat_key, lon_key))
                    if up_params is not None:
                        cell_params = up_params
                        n_upstream += 1
                    else:
                        n_own += 1
                except (ValueError, IndexError):
                    n_own += 1

            for key, idx in INDEX_MAP.items():
                if key in cell_params:
                    parts[idx] = f"{cell_params[key]:.3f}"
            f_out.write(" ".join(parts) + "\n")

    if upstream_mask:
        logger.info("Composite soil write: %d upstream cells, %d own cells",
                     n_upstream, n_own)


def write_global_param(src_path: str, dst_path: str,
                       soil_path: str, result_dir: str) -> None:
    with open(src_path, "r", encoding="utf-8", errors="ignore") as f_in, \
         open(dst_path, "w", encoding="utf-8") as f_out:
        for line in f_in:
            stripped = line.strip()
            if stripped.startswith("SOIL"):
                f_out.write(f"SOIL                    {soil_path}\n")
            elif stripped.startswith("RESULT_DIR"):
                rd = result_dir if result_dir.endswith("/") else result_dir + "/"
                f_out.write(f"RESULT_DIR              {rd}\n")
            else:
                f_out.write(line)


# ── VIC output preprocessing ────────────────────────────────────────────

def preprocess_vic_output(vic_dir: str, out_dir: str) -> None:
    ensure_dir(out_dir)
    for name in os.listdir(vic_dir):
        if not name.endswith(".txt") or name.startswith("._"):
            continue
        src = os.path.join(vic_dir, name)
        dst = os.path.join(out_dir, name[:-4])
        with open(src, "r", encoding="utf-8", errors="ignore") as f_in, \
             open(dst, "w", encoding="utf-8") as f_out:
            for _ in range(3):
                next(f_in, None)
            for line in f_in:
                parts = line.strip().split()
                if len(parts) < 19:
                    continue
                out = [parts[0], parts[1], parts[2], parts[3],
                       parts[18], parts[16], parts[17]]
                f_out.write("\t".join(out) + "\n")


# ── routing directory preparation ────────────────────────────────────────

def _find_first_by_suffix(files: List[str], suffix: str) -> str:
    for n in sorted(files):
        if n.endswith(suffix):
            return n
    return ""


def prepare_routing_dir(run_dir: str, vic_for_routing: str, routing_result: str,
                        start_year: int, end_year: int,
                        rout_template_dir: str, flux_prefix: str,
                        routing_params: Dict[str, float] = None,
                        upstream_routing_info: Optional[List[UpstreamRoutingInfo]] = None,
                        ) -> str:
    routing_dir = os.path.join(run_dir, "rout")
    ensure_dir(routing_dir)

    tmpl_files = [n for n in os.listdir(rout_template_dir) if not n.startswith("._")]
    for fname in tmpl_files:
        src = os.path.join(rout_template_dir, fname)
        dst = os.path.join(routing_dir, fname)
        if os.path.isfile(src):
            shutil.copyfile(src, dst)

    # Symlink VIC input / routing output
    for link_name, target in [("vic_in", vic_for_routing), ("rout_out", routing_result)]:
        link_path = os.path.join(routing_dir, link_name)
        if os.path.islink(link_path) or os.path.exists(link_path):
            try:
                os.unlink(link_path)
            except Exception:
                pass
        os.symlink(target, link_path)

    # Find rout_global.txt
    global_candidates = [
        n for n in os.listdir(routing_dir)
        if n.endswith("rout_global.txt") and not n.startswith("._")
    ]
    if not global_candidates:
        raise FileNotFoundError(f"rout_global.txt not found in template dir: {rout_template_dir}")
    global_path = os.path.join(routing_dir, sorted(global_candidates)[0])

    routed_files = [n for n in os.listdir(routing_dir) if not n.startswith("._")]
    direc = _find_first_by_suffix(routed_files, "_direc.txt")
    veloc = _find_first_by_suffix(routed_files, "_veloc.txt")
    diff = _find_first_by_suffix(routed_files, "_diff.txt")
    xmask = _find_first_by_suffix(routed_files, "_xmask.txt")
    frac = _find_first_by_suffix(routed_files, "_frac.txt")
    staloc = _find_first_by_suffix(routed_files, "_staloc.txt")
    uh_all = "UH.all" if "UH.all" in routed_files else _find_first_by_suffix(routed_files, ".all")

    with open(global_path, "r", encoding="utf-8", errors="ignore") as f_in:
        lines = f_in.readlines()

    out_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.endswith("_direc.txt") and direc:
            out_lines.append(f"./{direc}\n")
        elif stripped.endswith("_veloc.txt") and veloc:
            out_lines.append(f"./{veloc}\n")
        elif stripped.endswith("_diff.txt") and diff:
            out_lines.append(f"./{diff}\n")
        elif stripped.endswith("_xmask.txt") and xmask:
            out_lines.append(f"./{xmask}\n")
        elif stripped.endswith("_frac.txt") and frac:
            out_lines.append(f"./{frac}\n")
        elif stripped.endswith("_staloc.txt") and staloc:
            out_lines.append(f"./{staloc}\n")
        elif "vic_in" in stripped and "fluxes_" in stripped:
            out_lines.append(f"./vic_in/{flux_prefix}\n")
        elif stripped.endswith("/rout_out/") or stripped.endswith("./rout_out/"):
            out_lines.append("./rout_out/\n")
        elif (stripped == "./UH.all" or stripped.endswith("UH.all")) and uh_all:
            out_lines.append(f"./{uh_all}\n")
        else:
            out_lines.append(line)

    # Replace time line
    time_line = f"{start_year} 01 {end_year} 12\n"
    new_lines = []
    for line in out_lines:
        parts = line.strip().split()
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            new_lines.append(time_line)
        else:
            new_lines.append(line)

    # Inject routing parameters (velocity / diffusivity) via grid files.
    #
    # Strategy: dynamically generate _veloc.txt and _diff.txt from _direc.txt,
    # then set rout_global.txt to reference them with .true.
    #
    # If upstream_routing_info is provided, generate composite grids where
    # upstream cells inherit upstream basins' calibrated values and only
    # this basin's own cells use the current routing_params.
    velocity = 1.5
    diffusivity = 800.0
    if routing_params:
        velocity = routing_params.get("velocity", velocity)
        diffusivity = routing_params.get("diffusivity", diffusivity)

    if direc:
        direc_path = os.path.join(routing_dir, direc)
        prefix = direc.replace("_direc.txt", "")
        veloc_name = f"{prefix}_veloc.txt"
        diff_name = f"{prefix}_diff.txt"
        veloc_path = os.path.join(routing_dir, veloc_name)
        diff_path = os.path.join(routing_dir, diff_name)

        if upstream_routing_info:
            generate_composite_grid(direc_path, veloc_path, velocity,
                                    upstream_routing_info, "velocity")
            generate_composite_grid(direc_path, diff_path, diffusivity,
                                    upstream_routing_info, "diffusivity")
        else:
            generate_uniform_grid(direc_path, veloc_path, velocity)
            generate_uniform_grid(direc_path, diff_path, diffusivity)

        # Rewrite velocity/diffusivity sections in rout_global.txt to use
        # .true. + grid file path (handles both .false./scalar and
        # .true./file templates transparently).
        final_lines = []
        expect_veloc_flag = False
        expect_veloc_val = False
        expect_diff_flag = False
        expect_diff_val = False
        for line in new_lines:
            stripped = line.strip()
            # Detect section headers by comment
            if "VELOCITY" in stripped.upper() and stripped.startswith("#"):
                final_lines.append(line)
                expect_veloc_flag = True
                continue
            if "DIFF" in stripped.upper() and stripped.startswith("#") and "DIREC" not in stripped.upper():
                final_lines.append(line)
                expect_diff_flag = True
                continue
            # Handle velocity flag → .true.
            if expect_veloc_flag:
                expect_veloc_flag = False
                expect_veloc_val = True
                final_lines.append(".true.\n")
                continue
            # Handle velocity value → file path
            if expect_veloc_val:
                expect_veloc_val = False
                final_lines.append(f"./{veloc_name}\n")
                continue
            # Handle diffusivity flag → .true.
            if expect_diff_flag:
                expect_diff_flag = False
                expect_diff_val = True
                final_lines.append(".true.\n")
                continue
            # Handle diffusivity value → file path
            if expect_diff_val:
                expect_diff_val = False
                final_lines.append(f"./{diff_name}\n")
                continue
            final_lines.append(line)
        new_lines = final_lines

    with open(global_path, "w", encoding="utf-8") as f_out:
        f_out.writelines(new_lines)

    # Remove stale .uh_s files
    for n in os.listdir(routing_dir):
        if n.endswith(".uh_s") and os.path.isfile(os.path.join(routing_dir, n)):
            try:
                os.remove(os.path.join(routing_dir, n))
            except Exception:
                pass

    ensure_dir(routing_result)
    return routing_dir


def find_day_file(routing_result: str) -> str:
    candidates = [
        n for n in os.listdir(routing_result)
        if n.endswith(".day") and not n.endswith("day_mm") and not n.startswith("._")
    ]
    if not candidates:
        return ""
    candidates.sort()
    return os.path.join(routing_result, candidates[0])


# ── VIC execution with watchdog ──────────────────────────────────────────

def run_vic_with_watchdog(cfg: RuntimeConfig, run_id: int,
                          global_path: str, log_path: str) -> bool:
    hard_timeout_sec = 4 * 3600
    poll_sec = 20
    heartbeat_sec = 60

    start = time.time()
    last_heartbeat = start
    proc = subprocess.Popen(
        [cfg.vic_exe, "-g", global_path],
        cwd=cfg.current_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    while True:
        if proc.poll() is not None:
            break
        now = time.time()
        if now - last_heartbeat >= heartbeat_sec:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"Run {run_id} VIC heartbeat: elapsed_sec={now - start:.0f}\n")
            last_heartbeat = now
        if time.time() - start > hard_timeout_sec:
            proc.kill()
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"Run {run_id} VIC timeout (> {hard_timeout_sec}s). Killed.\n")
            return False
        time.sleep(poll_sec)

    if proc.returncode != 0:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"Run {run_id} VIC failed. returncode={proc.returncode}\n")
        return False
    return True


# ── main entry point: run_once ───────────────────────────────────────────

def run_once(cfg: RuntimeConfig, run_id: int, params: Dict[str, float],
             phase: str, log_path: str,
             routing_params: Dict[str, float] = None,
             upstream_routing_info: Optional[List[UpstreamRoutingInfo]] = None,
             upstream_soil_info: Optional[List[UpstreamSoilInfo]] = None,
             vic_flux_source: str = "",
             ) -> Tuple[bool, Dict[str, float]]:
    """Run VIC + routing for one candidate.  Thread-safe — uses only cfg paths.

    If *vic_flux_source* is a non-empty path, skip VIC execution and symlink
    the pre-existing flux directory as vic_for_routing (routing-only mode).
    """
    current = cfg.current_dir
    reset_dir(current)

    vic_for_routing = os.path.join(current, "vic_for_routing")
    routing_result = os.path.join(current, "routing_result")

    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"Run {run_id} start: phase={phase}, params={params}\n")
        if routing_params:
            log.write(f"  routing_params: {routing_params}\n")

    if vic_flux_source and os.path.isdir(vic_flux_source):
        # Routing-only mode: skip VIC, reuse existing flux output
        os.symlink(os.path.abspath(vic_flux_source), vic_for_routing)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"  routing-only: using flux from {vic_flux_source}\n")
    else:
        # Normal mode: run VIC
        vic_result = os.path.join(current, "vic_result")
        ensure_dir(vic_result)

        soil_path = os.path.join(current, "SOIL_PARAM_COMPLETE.txt")
        write_soil_params(cfg.soil_base, soil_path, params,
                          upstream_soil_infos=upstream_soil_info)

        global_path = os.path.join(current, "global_param.txt")
        write_global_param(cfg.global_base, global_path, soil_path, vic_result)

        if not run_vic_with_watchdog(cfg, run_id, global_path, log_path):
            return False, {}

        preprocess_vic_output(vic_result, vic_for_routing)
    routing_dir = prepare_routing_dir(
        current, vic_for_routing, routing_result,
        cfg.start_year, cfg.end_year,
        rout_template_dir=cfg.rout_template_dir,
        flux_prefix=cfg.flux_prefix,
        routing_params=routing_params,
        upstream_routing_info=upstream_routing_info,
    )

    rout_proc = subprocess.run(
        [cfg.rout_exe, "rout_global.txt"], cwd=routing_dir,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if rout_proc.returncode != 0:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"Run {run_id} routing failed: {rout_proc.stderr}\n")
        return False, {}

    day_file = find_day_file(routing_result)
    if not day_file:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"Run {run_id} missing routing day file.\n")
        return False, {}

    start_date = dt.date(cfg.start_year, 1, 1)
    end_date = dt.date(cfg.end_year, 12, 31)
    obs = read_obs(cfg.obs_file, start_date, end_date)
    sim = read_sim_day(day_file, start_date, end_date)
    if is_monthly_obs(obs):
        sim = aggregate_sim_to_monthly(sim)
    nse, rmse, pbias, kge, peak_ratio, low_ratio = compute_metrics(obs, sim)

    metrics = {
        "NSE": nse,
        "KGE": kge,
        "RMSE": rmse,
        "PBIAS": pbias,
        "peak_ratio": peak_ratio,
        "lowflow_ratio": low_ratio,
    }
    # Extended hydrologic signatures
    sigs = compute_signatures(obs, sim)
    metrics.update(sigs)

    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"Run {run_id} done: NSE={nse:.4f}, RMSE={rmse:.4f}, PBIAS={pbias:.2f}%\n")

    return True, metrics
