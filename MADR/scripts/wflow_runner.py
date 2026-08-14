#!/usr/bin/env python3
"""Lower-level wflow_sbm wrapper: writes calibrated parameters into a per-run
copy of the wflow model's ``staticmaps.nc`` (and patches ``sbm_config.toml``),
runs Wflow.jl via Julia, parses per-gauge discharge, and computes performance
metrics.

Mirrors ``swatplus_runner.py`` EXACTLY in structure (run_once_batched, the
exclusive-set logic, obs parsing, metric computation). The only substantive
difference from SWAT+ is HOW parameters are applied: instead of writing a
``calibration.cal`` text file, this module edits the gridded NetCDF static
maps per grid cell and repoints a few scalar config entries at newly-created
per-cell maps.

wflow_sbm has integrated kinematic-wave routing, so daily discharge per gauge
is read directly from the model's gauge-discharge CSV (one ``Q_<gauge_id>``
column per gauge).

Parameter application (see write_staticmaps_batched):
  * KsatVer / f / SoilThickness  -> multiply the base map cell-wise ("mult")
  * KsatHorFrac / N / N_River / TT / Cfmax -> set per-cell value ("absval");
    these are scalars (KsatHorFrac / N / N_River) or default-only snow params
    (TT / Cfmax, absent from [input.static]) in the base config today, so we
    CREATE per-cell maps and repoint (or add) the config entries at them.

Per-worker parameters are scoped to DISJOINT grid-cell sets
(compute_exclusive_hru_sets on cell ids), exactly like SWAT+'s HRU locking:
the inner (upstream) worker keeps its full cell set; the outer (downstream)
worker's cells have every inner-worker cell removed, so each cell receives
exactly one worker's parameters.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .streamflow_metrics import compute_all_metrics  # shared metric math

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Batched per-worker spec (used by run_once_batched)
# ---------------------------------------------------------------------------

@dataclass
class WorkerSpec:
    """Per-worker info bundled into a single batched wflow_sbm run.

    Field name ``hru_fids`` is kept identical to the SWAT+ WorkerSpec so the
    shared exclusive-set helper works verbatim; for wflow these are LINEAR
    GRID-CELL IDS (row * ncol + col over the staticmaps grid), not SWAT HRU
    FIDs. ``gauge_id`` selects the ``Q_<gauge_id>`` column in the gauge CSV.
    """
    subbasin_id: str
    hru_fids: List[int]
    obs_file: str
    start_date: str = "1987-01-01"
    end_date: str = "2000-12-31"
    gauge_id: int = 0


# ---------------------------------------------------------------------------
# Exclusive cell sets (VERBATIM from swatplus_runner.compute_exclusive_hru_sets)
# ---------------------------------------------------------------------------

def compute_exclusive_hru_sets(
    apply_order: List[str],
    hru_by_sid: Dict[str, List[int]],
) -> Dict[str, List[int]]:
    """Return per-sid cell lists with INNER gauges' cells removed from OUTER.

    *apply_order* is the outer-first ordering. For nested basins this yields:

        outer cells minus union(every later gauge in apply_order).
        inner cells unchanged.

    The result is that every cell appears in AT MOST ONE block, so each
    worker's parameters are applied to that cell exactly once. Identical logic
    to the SWAT+ HRU-exclusivity used for nested locking.
    """
    exclusive: Dict[str, List[int]] = {}
    later_union: set = set()
    for sid in reversed(apply_order):
        hrus = hru_by_sid.get(sid, []) or []
        own = sorted(set(int(h) for h in hrus) - later_union)
        exclusive[sid] = own
        later_union.update(int(h) for h in hrus)
    return exclusive


# ---------------------------------------------------------------------------
# Working-copy management (mirrors swatplus_runner)
# ---------------------------------------------------------------------------

# Output files/dirs from a previous wflow run that should NOT be copied (they
# will be regenerated). Skipping the data/output tree cuts a lot of wall-time.
_SKIP_COPY_DIRS = {"data", "results", "scripts", "__pycache__"}
_SKIP_COPY_PATTERNS = (".log", "outstates.nc")


def _should_skip(name: str) -> bool:
    if name in _SKIP_COPY_DIRS:
        return True
    for pat in _SKIP_COPY_PATTERNS:
        if name.endswith(pat):
            return True
    return False


def _ignore_outputs(_src: str, names: List[str]) -> List[str]:
    return [n for n in names if _should_skip(n)]


_STAGED_CACHES: Dict[str, Path] = {}
_STAGE_LOCK = threading.Lock()


def _stage_template_local(template_dir: Path, work_root: Path) -> Path:
    """Stage a local cache of the wflow model dir once.

    Subsequent per-run copies are made local-to-local (fast). Mirrors the
    SWAT+ template staging.
    """
    template_key = str(template_dir.resolve())
    cached = _STAGED_CACHES.get(template_key)
    if cached is not None and (cached / ".staged_ok").exists():
        return cached

    with _STAGE_LOCK:
        cached = _STAGED_CACHES.get(template_key)
        if cached is not None and (cached / ".staged_ok").exists():
            return cached

        import hashlib
        digest = hashlib.sha1(template_key.encode("utf-8")).hexdigest()[:12]
        cache_root = work_root.parent / "_wflow_template_cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        cache = cache_root / digest
        marker = cache / ".staged_ok"
        if marker.exists():
            _STAGED_CACHES[template_key] = cache
            return cache
        if cache.exists():
            shutil.rmtree(cache)
        cache.mkdir(parents=True, exist_ok=True)
        logger.info("Staging wflow model -> %s (one-time copy from %s)", cache, template_dir)
        shutil.copytree(
            str(template_dir),
            str(cache),
            symlinks=True,
            ignore=_ignore_outputs,
            dirs_exist_ok=True,
        )
        marker.write_text("ok")
        _STAGED_CACHES[template_key] = cache
        return cache


def _make_working_model(template_dir: Path, work_root: Path, run_tag: str) -> Path:
    """Create a per-run working copy of the wflow model directory."""
    local_template = _stage_template_local(template_dir, work_root)
    target = work_root / f"wflow_{run_tag}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        str(local_template),
        str(target),
        symlinks=True,
        ignore=_ignore_outputs,
    )
    return target


# ---------------------------------------------------------------------------
# Parameter application into staticmaps.nc + sbm_config.toml
# ---------------------------------------------------------------------------

# Base-config scalar keys (in [input.static], written as `<key>.value`) that we
# repoint at newly-created per-cell maps once a worker sets them. The map name
# equals the wflow_sbm parameter name.
_SCALAR_CONFIG_KEYS = {
    "KsatHorFrac": "subsurface_water__horizontal_to_vertical_saturated_hydraulic_conductivity_ratio",
    "N":           "land_surface_water_flow__manning_n_parameter",
    "N_River":     "river_water_flow__manning_n_parameter",
    # Snow params. In the built Liao config these are NOT present in
    # [input.static] (wflow uses its internal defaults -- see run.log lines
    # "Set snowpack__degree_day_coefficient using default value 3.75." and
    # "Set atmosphere_air__snowfall_temperature_threshold using default value
    # 0.0."). We create per-cell maps and add these mappings to [input.static]
    # so wflow reads the calibrated snow fields instead of the defaults. Names
    # are the exact wflow v1.x standard variable names.
    "TT":          "atmosphere_air__snowfall_temperature_threshold",
    "Cfmax":       "snowpack__degree_day_coefficient",
}


def _load_cell_area(subbasin_attrs_file: Path) -> Dict[int, float]:
    """Load {cell_id: area_km2} from the subbasin_attrs JSON (optional).

    Not needed for discharge (wflow routes internally and reports m3/s at each
    gauge) but kept for parity with the SWAT+ area map and for diagnostics.
    """
    try:
        attrs = json.loads(Path(subbasin_attrs_file).read_text())
    except Exception:
        return {}
    out: Dict[int, float] = {}
    for h in attrs:
        cid = h.get("cell_id")
        if cid is None:
            continue
        out[int(cid)] = float(h.get("area_km2", h.get("area", 0.0)) or 0.0)
    return out


def write_staticmaps_batched(
    model_dir: Path,
    per_worker_blocks: List[Tuple[str, Dict[str, float], List[int], Dict[str, str]]],
) -> None:
    """Apply per-worker params to disjoint grid cells in the per-run model.

    Each entry of *per_worker_blocks* is ``(sid, params, cell_ids, change_types)``:

    * ``sid``          — sub-basin id (inline logging only).
    * ``params``       — ``{param_name: value}`` for that worker.
    * ``cell_ids``     — EXCLUSIVE linear cell ids the params apply to (may be
                         empty; empty blocks are skipped).
    * ``change_types`` — ``{param_name: "mult" | "absval"}``.

    Behaviour per parameter:
      * "mult" (KsatVer, f, SoilThickness): multiply the base map at the
        worker's cells by the value.
      * "absval" (KsatHorFrac, N, N_River, TT, Cfmax): set the worker's cells
        to the value in a per-cell map created for that parameter (initialized
        from the base scalar `.value`, or the wflow default for the snow params
        TT / Cfmax which have no `.value` line, so untouched cells keep the base
        value), and repoint/add the sbm_config.toml [input.static] entry at the
        map.

    Cells outside the basin stay NaN (matching the build's convention, which
    ties every map's NaN pattern to ``wflow_ldd``).
    """
    import numpy as np
    import xarray as xr

    sm_path = model_dir / "staticmaps.nc"
    ds = xr.load_dataset(sm_path)  # load (not open_dataset) so we can overwrite in place

    ldd = ds["wflow_ldd"].values
    in_basin = ~np.isnan(ldd.astype(float))
    nrow, ncol = ldd.shape

    def _cells_to_mask(cell_ids: List[int]) -> "np.ndarray":
        m = np.zeros((nrow, ncol), dtype=bool)
        for cid in cell_ids:
            r, c = divmod(int(cid), ncol)
            if 0 <= r < nrow and 0 <= c < ncol:
                m[r, c] = True
        return m

    # Read the base scalar defaults from the config so per-cell "absval" maps
    # start from the base value on untouched cells.
    cfg_text = (model_dir / "sbm_config.toml").read_text()
    scalar_defaults = {
        "KsatHorFrac": _read_scalar_from_config(cfg_text, _SCALAR_CONFIG_KEYS["KsatHorFrac"], 100.0),
        "N":           _read_scalar_from_config(cfg_text, _SCALAR_CONFIG_KEYS["N"], 0.15),
        "N_River":     _read_scalar_from_config(cfg_text, _SCALAR_CONFIG_KEYS["N_River"], 0.075),
        # Snow params default to wflow's internal defaults (TT 0.0, Cfmax 3.75);
        # untouched cells keep these so behaviour is unchanged if a worker never
        # tunes snow. _read_scalar_from_config returns the fallback here because
        # these keys have no ".value" line in the base config.
        "TT":          _read_scalar_from_config(cfg_text, _SCALAR_CONFIG_KEYS["TT"], 0.0),
        "Cfmax":       _read_scalar_from_config(cfg_text, _SCALAR_CONFIG_KEYS["Cfmax"], 3.75),
    }

    # Track which scalar params were actually set (so we only repoint those).
    scalar_maps_created: Dict[str, "np.ndarray"] = {}

    def _get_or_init_scalar_map(name: str) -> "np.ndarray":
        if name in scalar_maps_created:
            return scalar_maps_created[name]
        # Initialize from base scalar inside the basin, NaN outside (match
        # KsatVer's NaN pattern which == ldd's).
        arr = np.full((nrow, ncol), np.nan, dtype=np.float32)
        arr[in_basin] = float(scalar_defaults[name])
        scalar_maps_created[name] = arr
        return arr

    # Apply each block on its exclusive cells.
    for sid, params, cell_ids, change_types in per_worker_blocks:
        if not cell_ids:
            continue
        mask = _cells_to_mask(cell_ids) & in_basin
        if not mask.any():
            continue
        for name, value in params.items():
            ctype = change_types.get(name, "absval")
            if name in ("KsatVer", "f", "SoilThickness") and ctype == "mult":
                base = ds[name].values.astype(np.float32)
                base[mask] = base[mask] * float(value)
                ds[name].values[...] = base
            elif name in _SCALAR_CONFIG_KEYS:
                arr = _get_or_init_scalar_map(name)
                arr[mask] = float(value)
            else:
                # Unknown / unexpected — skip defensively (do not corrupt maps).
                logger.warning("wflow: skipping unhandled param %s (ctype=%s)", name, ctype)

    # Add the created per-cell scalar maps to the dataset with the same NaN
    # convention as the existing float maps (_FillValue = -9999.0 in encoding).
    for name, arr in scalar_maps_created.items():
        da = xr.DataArray(arr, dims=("lat", "lon"),
                          coords={"lat": ds["lat"], "lon": ds["lon"]})
        da.encoding = {"_FillValue": -9999.0, "dtype": "float32"}
        ds[name] = da

    ds.to_netcdf(sm_path)
    ds.close()

    # Repoint the config at the created maps (only for scalars actually set).
    if scalar_maps_created:
        patch_sbm_config(model_dir / "sbm_config.toml", list(scalar_maps_created.keys()))


def _read_scalar_from_config(cfg_text: str, config_key: str, default: float) -> float:
    """Read ``<config_key>.value = <float>`` from the [input.static] block."""
    m = re.search(rf"^{re.escape(config_key)}\.value\s*=\s*([-\d.eE+]+)",
                  cfg_text, flags=re.MULTILINE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


def patch_sbm_config(config_path: Path, scalar_params: List[str]) -> None:
    """Repoint ``[input.static]`` scalar entries at the new per-cell maps.

    For each parameter in *scalar_params*, rewrite the line
        ``<config_key>.value = <x>``
    to
        ``<config_key> = "<param_name>"``
    so wflow reads the newly-created map instead of the scalar.
    """
    text = config_path.read_text()
    for pname in scalar_params:
        config_key = _SCALAR_CONFIG_KEYS.get(pname)
        if not config_key:
            continue
        pattern = rf"^{re.escape(config_key)}\.value\s*=.*$"
        replacement = f'{config_key} = "{pname}"'
        new_text, n = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
        if n == 0:
            # Already a map (or absent): make sure a map mapping exists.
            if re.search(rf"^{re.escape(config_key)}\s*=", text, flags=re.MULTILINE) is None:
                # Append under [input.static]. Insert after the header line.
                new_text = re.sub(r"(^\[input\.static\]\s*$)",
                                  r"\1\n" + replacement, text,
                                  count=1, flags=re.MULTILINE)
        text = new_text
    config_path.write_text(text)


def patch_config_dates(config_path: Path, start_date: str, end_date: str) -> None:
    """Set ``[time]`` starttime/endtime in sbm_config.toml (whole-day)."""
    text = config_path.read_text()
    st = f"{start_date}T00:00:00"
    et = f"{end_date}T00:00:00"
    text = re.sub(r'^starttime\s*=.*$', f'starttime = "{st}"', text,
                  count=1, flags=re.MULTILINE)
    text = re.sub(r'^endtime\s*=.*$', f'endtime = "{et}"', text,
                  count=1, flags=re.MULTILINE)
    config_path.write_text(text)


# ---------------------------------------------------------------------------
# Gauge-discharge CSV parser
# ---------------------------------------------------------------------------

# The config writes the gauge CSV to `<dir_output>/gauge_q.csv`, and wflow
# nests dir_output under the run dir, so the file lands at
# `data/output/data/output/gauge_q.csv` relative to the model dir.
_GAUGE_CSV_RELPATH = Path("data/output/data/output/gauge_q.csv")


def parse_gauge_csv(
    csv_path: Path,
    gauge_id: int,
    start_date: str = "",
    end_date: str = "",
) -> List[Tuple[str, float]]:
    """Return [(date_str, Q_m3s)] for one gauge from wflow's gauge CSV.

    CSV format: ``time,Q_1,Q_2,...`` where ``Q_<gauge_id>`` is the routed
    discharge (m3/s) at that gauge. The ``time`` column is ISO
    (``YYYY-MM-DDT00:00:00``).
    """
    if not csv_path.exists():
        logger.warning("gauge_q.csv not found: %s", csv_path)
        return []
    lines = csv_path.read_text().strip().split("\n")
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    col = f"Q_{int(gauge_id)}"
    if col not in header:
        # Single-gauge models may emit a bare "Q" column.
        if "Q" in header and len(header) == 2:
            col = "Q"
        else:
            logger.warning("gauge column %s not in gauge_q.csv header %s", col, header)
            return []
    q_i = header.index(col)
    try:
        t_i = header.index("time")
    except ValueError:
        t_i = 0
    start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
    out: List[Tuple[str, float]] = []
    for ln in lines[1:]:
        tok = ln.split(",")
        if len(tok) <= max(q_i, t_i):
            continue
        ts = tok[t_i].strip()
        date_str = ts.split("T")[0]
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            q = float(tok[q_i])
        except (ValueError, IndexError):
            continue
        if (start and dt < start) or (end and dt > end):
            continue
        out.append((date_str, q))
    return out


# ---------------------------------------------------------------------------
# Observed-flow parser (VERBATIM from swatplus_runner.parse_obs_file)
# ---------------------------------------------------------------------------

def _parse_obs_date(date_str: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized obs date: {date_str}")


def parse_obs_file(obs_path: Path) -> Dict[str, float]:
    """Parse observed-flow file into {date_str (YYYY-MM-DD): flow}.

    Handles the real Huaihe 5-column format (latin1/CRLF/tab; stcd,dates,z,Q,
    name) AND the simpler ``date,flow`` / ``date  flow`` formats (the Liao obs
    are ``date,Q_m3s``). Filters -99 / negative sentinels. Verbatim copy of
    the SWAT+ parser so both models share obs handling.
    """
    if not obs_path.exists():
        return {}
    try:
        raw = obs_path.read_bytes().decode("latin1", errors="replace")
    except Exception:
        raw = obs_path.read_text(errors="replace")
    raw_lines = raw.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")
    if not raw_lines:
        return {}

    out: Dict[str, float] = {}
    header_toks = raw_lines[0].split("\t")
    looks_like_real_format = (
        len(header_toks) >= 4
        and "dates" in [t.strip().lower() for t in header_toks]
        and "q" in [t.strip().lower() for t in header_toks]
    )

    if looks_like_real_format:
        cols = {t.strip().lower(): i for i, t in enumerate(header_toks)}
        d_i = cols["dates"]
        q_i = cols["q"]
        for line in raw_lines[1:]:
            parts = line.split("\t")
            if len(parts) <= max(d_i, q_i):
                continue
            date_str = parts[d_i].strip()
            q_str = parts[q_i].strip()
            try:
                dt = _parse_obs_date(date_str)
                flow = float(q_str)
                if flow < 0:
                    continue
                out[dt.strftime("%Y-%m-%d")] = flow
            except (ValueError, IndexError):
                continue
        return out

    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            continue
        date_str = parts[0]
        try:
            dt = _parse_obs_date(date_str)
            flow = float(parts[1])
            if flow < 0:
                continue
            out[dt.strftime("%Y-%m-%d")] = flow
        except (ValueError, IndexError):
            continue
    return out


def aggregate_sim_to_monthly(sim_daily: Dict[str, float]) -> Dict[str, float]:
    bucket: Dict[str, List[float]] = {}
    for d, v in sim_daily.items():
        if math.isnan(v) or math.isinf(v):
            continue
        bucket.setdefault(d[:7] + "-01", []).append(v)
    return {k: (sum(vs) / len(vs)) for k, vs in bucket.items() if vs}


def is_monthly_obs(obs: Dict[str, float]) -> bool:
    if not obs:
        return False
    return all(d.endswith("-01") for d in obs.keys())


def _filter_metrics_window(
    common: List[str], eval_start: str, eval_end: str, monthly: bool, label: str,
) -> List[str]:
    if not (eval_start or eval_end):
        return common
    lo = eval_start
    if lo and monthly:
        lo = lo[:7] + "-01"
    kept = [d for d in common
            if (not lo or d >= lo) and (not eval_end or d <= eval_end)]
    if len(kept) != len(common):
        logger.info("Evaluation window [%s..%s] keeps %d/%d dates for %s.",
                    eval_start or "-", eval_end or "-", len(kept), len(common), label)
    if not kept:
        logger.warning("Evaluation window [%s..%s] leaves no sim/obs overlap for %s.",
                       eval_start or "-", eval_end or "-", label)
    return kept


# ---------------------------------------------------------------------------
# Julia run helper
# ---------------------------------------------------------------------------

def _run_wflow_julia(run_cmd: str, config_path: Path, work_dir: Path,
                     log_path: str, run_id: int, timeout: int = 7200) -> Tuple[bool, str]:
    """Run Wflow.jl for *config_path* using the configured Julia command.

    The command may contain ``{config}`` which is substituted with the config
    path. If it does not, the config path is appended as a final argument
    (matching the documented Julia one-liner form).
    """
    if "{config}" in run_cmd:
        cmd = run_cmd.format(config=str(config_path))
    elif "sbm_config.toml" in run_cmd or "Wflow.run(" in run_cmd:
        # The template already embeds a config path; replace any placeholder
        # config token with our per-run config.
        cmd = re.sub(r'Wflow\.run\("[^"]*"\)',
                     f'Wflow.run("{config_path}")', run_cmd)
    else:
        cmd = f'{run_cmd} "{config_path}"'

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(work_dir),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("wflow (Julia) timed out for run %d", run_id)
        return False, "TIMEOUT"
    elapsed = time.time() - t0
    log_text = (
        f"=== wflow run {run_id} ===\n"
        f"cmd: {cmd}\nwork_dir: {work_dir}\n"
        f"exit_code: {proc.returncode}\nelapsed: {elapsed:.1f}s\n"
        f"\n--- STDOUT(tail) ---\n{proc.stdout[-3000:]}\n"
        f"--- STDERR(tail) ---\n{proc.stderr[-3000:]}\n"
    )
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(log_text)
    except Exception:
        pass
    if proc.returncode != 0:
        logger.error("wflow (Julia) exit code %d for run %d", proc.returncode, run_id)
        return False, proc.stderr[-2000:]
    return True, ""


# ---------------------------------------------------------------------------
# Daily-discharge CSV writer (mirrors swatplus_runner)
# ---------------------------------------------------------------------------

def _write_discharge_csv(csv_path: Path, sim_by_date: Dict[str, float],
                         obs_by_date: Dict[str, float],
                         start_date: str, end_date: str) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        if not sim_by_date:
            return
        keys = sorted(sim_by_date.keys())
        start = datetime.strptime(keys[0], "%Y-%m-%d")
        end = datetime.strptime(keys[-1], "%Y-%m-%d")
    lines = ["date,Q_sim_m3s,Q_obs_m3s"]
    cur = start
    while cur <= end:
        key = cur.strftime("%Y-%m-%d")
        sim_v = sim_by_date.get(key)
        obs_v = obs_by_date.get(key)
        sim_s = f"{sim_v:.4f}" if sim_v is not None else ""
        obs_s = f"{obs_v:.4f}" if obs_v is not None else ""
        lines.append(f"{key},{sim_s},{obs_s}")
        cur += timedelta(days=1)
    csv_path.write_text("\n".join(lines) + "\n")


def _batched_nan_metrics() -> Dict[str, float]:
    return {
        "NSE": float("nan"), "KGE": float("nan"), "PBIAS": float("nan"),
        "RMSE": float("nan"), "R2": float("nan"),
        "peak_ratio": float("nan"), "lowflow_ratio": float("nan"),
        "peak_timing_bias_days": float("nan"), "peak_magnitude_bias_pct": float("nan"),
        "recession_k_ratio": float("nan"), "baseflow_index_bias_pct": float("nan"),
    }


# ---------------------------------------------------------------------------
# One full wflow run (single-worker; fallback path)
# ---------------------------------------------------------------------------

def run_once(
    cfg,
    run_id: int,
    params: Dict[str, float],
    phase_tag: str,
    log_path: str,
    output_csv_path: Optional[str] = None,
    cleanup: bool = True,
) -> Tuple[bool, Dict[str, float]]:
    """Execute wflow_sbm once for the given config and parameter set.

    Applies *params* to the whole model domain (single-worker fallback; the
    normal nested path is run_once_batched). Returns (ok, metrics).
    """
    ok, metrics_by_sid, _sim, _obs = run_once_batched(
        base_cfg=cfg,
        run_id=run_id,
        per_worker_params={cfg.basin_name: params},
        worker_specs=[WorkerSpec(
            subbasin_id=cfg.basin_name,
            hru_fids=list(getattr(cfg, "cell_ids", [])),
            obs_file=cfg.obs_file,
            start_date=cfg.start_date,
            end_date=cfg.end_date,
            gauge_id=int(getattr(cfg, "gauge_id", 0) or 0),
        )],
        apply_order=[cfg.basin_name],
        phase_tag=phase_tag,
        log_path=log_path,
        output_csv_paths=({cfg.basin_name: output_csv_path}
                          if output_csv_path else None),
        change_types=getattr(cfg, "change_types", None),
        cleanup=cleanup,
    )
    return ok, metrics_by_sid.get(cfg.basin_name, _batched_nan_metrics())


# ---------------------------------------------------------------------------
# Batched single-run entry point (one wflow call covers all sub-basins)
# ---------------------------------------------------------------------------

def run_once_batched(
    base_cfg,
    run_id: int,
    per_worker_params: Dict[str, Dict[str, float]],
    worker_specs: List[WorkerSpec],
    apply_order: List[str],
    phase_tag: str,
    log_path: str,
    output_csv_paths: Optional[Dict[str, str]] = None,
    change_types: Optional[Dict[str, str]] = None,
    cleanup: bool = True,
) -> Tuple[bool, Dict[str, Dict[str, float]],
           Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    """Run wflow_sbm exactly once with every worker's params on disjoint cells.

    Parameters mirror ``swatplus_runner.run_once_batched``:

    base_cfg
        Object exposing basin-wide fields: ``basin_name``, ``base_output_dir``,
        ``wflow_model_dir``, ``wflow_run_cmd``, ``change_types``,
        ``start_date`` / ``end_date`` (and optionally ``eval_start_date`` /
        ``eval_end_date`` attached dynamically by graph_nodes).
    per_worker_params
        ``{subbasin_id: {param: value}}``.
    worker_specs
        One WorkerSpec per participating worker (cell ids + obs + gauge id).
    apply_order
        Outer-first ordering; inner cells kept, outer cells minus inner (see
        compute_exclusive_hru_sets).

    Returns ``(ok, metrics_by_sid, sim_by_sid, obs_by_sid)``.
    """
    ct = dict(getattr(base_cfg, "change_types", {}) or {})
    if change_types:
        ct.update(change_types)

    # Exclusive cell sets so each cell is touched by at most one block.
    cells_by_sid: Dict[str, List[int]] = {
        s.subbasin_id: [int(x) for x in s.hru_fids] for s in worker_specs
    }
    exclusive_cells = compute_exclusive_hru_sets(apply_order, cells_by_sid)

    # Build per-worker blocks preserving apply_order.
    blocks: List[Tuple[str, Dict[str, float], List[int], Dict[str, str]]] = []
    sid_to_spec = {s.subbasin_id: s for s in worker_specs}
    for sid in apply_order:
        if sid not in per_worker_params or sid not in sid_to_spec:
            continue
        cells = exclusive_cells.get(sid, [])
        blocks.append((sid, per_worker_params[sid], cells, ct))

    # ─── Stage the model dir ──────────────────────────────────────────
    template = Path(base_cfg.wflow_model_dir)
    work_root = Path(base_cfg.base_output_dir) / f"{base_cfg.basin_name}_wflow_cali_batched"
    work_root.mkdir(parents=True, exist_ok=True)
    run_tag = f"run{run_id}_{phase_tag}".replace(" ", "_")
    work_dir = _make_working_model(template, work_root, run_tag)

    config_path = work_dir / "sbm_config.toml"

    # ─── Apply parameters + set simulation dates ──────────────────────
    try:
        write_staticmaps_batched(work_dir, blocks)
        patch_config_dates(config_path, str(base_cfg.start_date), str(base_cfg.end_date))
    except Exception as e:
        logger.error("wflow param application failed for run %d: %s", run_id, e)
        import traceback
        logger.error(traceback.format_exc())
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        nan = _batched_nan_metrics()
        return False, {s.subbasin_id: dict(nan) for s in worker_specs}, {}, {}

    # ─── Run Wflow.jl ─────────────────────────────────────────────────
    ok, err = _run_wflow_julia(
        str(base_cfg.wflow_run_cmd), config_path, work_dir, log_path, run_id)
    if not ok:
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        nan = _batched_nan_metrics()
        return False, {s.subbasin_id: dict(nan) for s in worker_specs}, {}, {}

    # ─── Parse gauge CSV once, extract per-gauge Q ────────────────────
    gauge_csv = work_dir / _GAUGE_CSV_RELPATH
    metrics_by_sid: Dict[str, Dict[str, float]] = {}
    sim_by_sid: Dict[str, Dict[str, float]] = {}
    obs_by_sid: Dict[str, Dict[str, float]] = {}

    eval_start = str(getattr(base_cfg, "eval_start_date", "") or "")
    eval_end = str(getattr(base_cfg, "eval_end_date", "") or "")

    for spec in worker_specs:
        sim_pairs = parse_gauge_csv(
            gauge_csv, int(spec.gauge_id),
            start_date=spec.start_date, end_date=spec.end_date,
        )
        sim_by_date = dict(sim_pairs)
        try:
            obs_by_date = parse_obs_file(Path(spec.obs_file)) if spec.obs_file else {}
        except Exception:
            obs_by_date = {}

        monthly = is_monthly_obs(obs_by_date)
        sim_for_metrics = sim_by_date
        if monthly:
            sim_for_metrics = aggregate_sim_to_monthly(sim_by_date)
        common = sorted(set(sim_for_metrics) & set(obs_by_date))
        common = _filter_metrics_window(common, eval_start, eval_end, monthly, spec.subbasin_id)
        if not common:
            metrics_by_sid[spec.subbasin_id] = _batched_nan_metrics()
        else:
            sim = [sim_for_metrics[d] for d in common]
            obs = [obs_by_date[d] for d in common]
            m = compute_all_metrics(sim, obs, common)
            m["n_days"] = len(common)
            metrics_by_sid[spec.subbasin_id] = m

        sim_by_sid[spec.subbasin_id] = sim_by_date
        obs_by_sid[spec.subbasin_id] = obs_by_date

        if output_csv_paths and output_csv_paths.get(spec.subbasin_id):
            try:
                _write_discharge_csv(
                    Path(output_csv_paths[spec.subbasin_id]),
                    sim_by_date, obs_by_date, spec.start_date, spec.end_date)
            except Exception as e:
                logger.warning("Failed to write discharge CSV for %s: %s",
                               spec.subbasin_id, e)

    if cleanup and work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)

    return True, metrics_by_sid, sim_by_sid, obs_by_sid
