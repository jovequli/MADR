#!/usr/bin/env python3
"""Lower-level SWAT+ wrapper: edits calibration.cal, runs the binary,
parses hru_wb_day.txt aggregated per gauge, and computes performance metrics.

Patterned after skills/swatplus-auto-run/tools/s8/run_swatplus.py and
the parse_nested_metrics.py from the previous nested-basin SWAT+ run.

This template uses HRU-only objects (no channel objects), so daily discharge
per gauge is derived by aggregating wateryld (mm) over the gauge's HRUs:
    Q_m3 = wateryld_mm * area_km2 * 1e3
    Q_sim_m3s = sum(Q_m3 over HRUs in gauge) / 86400
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Batched per-worker spec (used by run_once_batched)
# ---------------------------------------------------------------------------

@dataclass
class WorkerSpec:
    """Per-worker info bundled into a single batched SWAT+ run.

    Each spec maps a worker's subbasin_id to:
      * its HRU/unit IDs in the SWAT+ template,
      * its observed-flow file,
      * the simulation period for metric computation.

    The subbasin_attrs_file (FID -> unit -> area_km2) is shared across
    workers in a single basin family and is therefore stored separately at
    the runtime-config level rather than per-worker here.
    """
    subbasin_id: str
    hru_fids: List[int]
    obs_file: str
    start_date: str = "1980-01-01"
    end_date: str = "1990-12-31"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# calibration.cal writer
# ---------------------------------------------------------------------------

# Object-type assignment per s6_generate_calibration_file.py.
# SWAT+ reads obj_typ from calibration.cal to route the change to the
# correct module (hru, bsn, aqu).
_OBJ_TYP = {
    "cn2":         "hru",
    "esco":        "hru",
    "epco":        "hru",
    "awc":         "hru",
    "perco":       "hru",
    "lat_ttime":   "hru",
    "canmx":       "hru",
    "surlag":      "bsn",
    "alpha":       "aqu",   # was alpha_bf in v1 — cal_parms.cal uses 'alpha'.
    "revap_co":    "aqu",
    "revap_min":   "aqu",
    "flo_min":     "aqu",
    "deep_seep":   "aqu",
    "snofall_tmp": "hru",
    "snomelt_tmp": "hru",
    "snomelt_max": "hru",
    "snomelt_min": "hru",
    "snomelt_lag": "hru",
}


# --- Name validation against the model's own cal_parms.cal ----------------
#
# SWAT+ silently DROPS any calibration.cal row whose name is not present in
# cal_parms.cal. That is how alpha_bf / gw_delay / rchg_dp went unnoticed in
# v1. Cache the valid name set per TxtInOut directory and abort the whole
# run on any unknown name.

_CAL_PARMS_NAME_CACHE: Dict[str, set] = {}


def _load_cal_parms_names(txtinout_dir: Path) -> set:
    """Return the set of valid calibration-name strings from cal_parms.cal.

    Format is fixed:
      line 1: header comment
      line 2: integer count
      line 3: column names (name obj_typ abs_min abs_max units)
      line 4+: data rows
    """
    key = str(txtinout_dir)
    if key in _CAL_PARMS_NAME_CACHE:
        return _CAL_PARMS_NAME_CACHE[key]
    cal_parms = txtinout_dir / "cal_parms.cal"
    if not cal_parms.exists():
        logger.warning("cal_parms.cal not found in %s; name validation disabled.", txtinout_dir)
        _CAL_PARMS_NAME_CACHE[key] = set()
        return _CAL_PARMS_NAME_CACHE[key]
    names = set()
    with cal_parms.open() as f:
        f.readline()  # header
        f.readline()  # count
        f.readline()  # column names
        for line in f:
            parts = line.split()
            if parts:
                names.add(parts[0])
    _CAL_PARMS_NAME_CACHE[key] = names
    return names


def validate_param_names(txtinout_dir: Path, proposed_names: List[str]) -> None:
    """Hard validation: every proposed name must exist in cal_parms.cal.

    Raises ``ValueError`` with the list of unknown names. Silent-drop bugs are
    the primary failure mode of SWAT+ calibration; we trade a clear crash for
    a wasted run.
    """
    valid = _load_cal_parms_names(txtinout_dir)
    if not valid:  # no cal_parms.cal -> skip silently
        return
    unknown = [n for n in proposed_names if n not in valid]
    if unknown:
        raise ValueError(
            f"calibration.cal includes name(s) not present in "
            f"{txtinout_dir/'cal_parms.cal'}: {unknown}. "
            f"SWAT+ would silently drop these rows."
        )


def write_calibration_cal(
    txtinout_dir: Path,
    params: Dict[str, float],
    change_types: Dict[str, str],
) -> None:
    """Write a SWAT+ calibration.cal applying each param globally.

    Format matches s6_generate_calibration_file.py (canonical 11-column layout).
    """
    validate_param_names(txtinout_dir, list(params.keys()))
    cal_path = txtinout_dir / "calibration.cal"
    rows = list(params.items())
    with cal_path.open("w") as f:
        f.write("calibration.cal: written by swatplus_runner.py\n")
        f.write(f"{len(rows)}\n")
        f.write(
            "  cal_parm       chg_typ     chg_val     conds     lyr1      lyr2      "
            "year1     year2     day1      day2      obj_tot\n"
        )
        for name, value in rows:
            ctype = change_types.get(name, "absval")
            # SWAT+ reads conds as integer; use 0 not "null"
            f.write(
                f"  {name:<14s} {ctype:<11s} {value:<12.4f} "
                f"{'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s}\n"
            )


# ---------------------------------------------------------------------------
# Batched calibration.cal writer (one row per (gauge, param) with HRU selector)
# ---------------------------------------------------------------------------

def write_calibration_cal_batched(
    txtinout_dir: Path,
    per_worker_blocks: List[Tuple[str, Dict[str, float], List[int], Dict[str, str]]],
) -> None:
    """Write a SWAT+ calibration.cal applying per-gauge params to HRU subsets.

    Each entry of *per_worker_blocks* is a tuple ``(sid, params, hrus, change_types)``:

    * ``sid``      — sub-basin id (used only for inline comments).
    * ``params``   — ``{param_name: value}`` for that gauge.
    * ``hrus``     — list of HRU/unit IDs the row should apply to. May be
                     empty (the gauge contributes nothing distinct, e.g. after
                     it has been fully eclipsed by inner gauges); empty blocks
                     are silently skipped.
    * ``change_types`` — ``{param_name: chg_typ}`` (absval / abschg / pctchg).

    Each row uses SWAT+'s HRU selector convention:
        obj_typ_present_in_obj_tot_field=count, followed by the HRU IDs on
        the same logical row (SWAT+ supports list-directed reads, so we put
        the HRU numbers right after the canonical 11 columns separated by
        spaces). For empty HRU lists the row is omitted entirely.

    Mathematical-equivalence note: callers should pass DISJOINT hru lists
    across blocks (use :func:`compute_exclusive_hru_sets`). For ``pctchg``
    rows, applying two overlapping blocks would compound multiplicatively
    rather than override.
    """
    # Validate every proposed name (across all worker blocks) against the
    # model's cal_parms.cal. Abort instead of silently dropping rows.
    all_names: List[str] = []
    for _sid, params, _hrus, _ct in per_worker_blocks:
        all_names.extend(params.keys())
    validate_param_names(txtinout_dir, all_names)

    cal_path = txtinout_dir / "calibration.cal"

    # Build the row list first so we can write the correct row count.
    #
    # Object-type routing rules (see _OBJ_TYP near the top of this module
    # and the SWAT+ source cal_parmchg_read.f90 / cal_parm_select.f90):
    #
    #   * HRU-typed params (cn2/esco/awc/...): one row per worker block,
    #     with the worker's exclusive HRU unit IDs listed after obj_tot=n.
    #   * bsn-typed params (surlag):           one GLOBAL row (obj_tot=0).
    #     The outermost (outlet) worker's value is used because SWAT+ would
    #     overwrite the basin-wide scalar with the last value anyway.
    #   * aqu-typed params (alpha_bf/gw_delay/revap_co/flo_min): one
    #     GLOBAL row (obj_tot=0). When the template has 0 aquifers,
    #     SWAT+ loops over zero elements (safe no-op); when it has
    #     aquifers, the param applies basin-wide.
    #
    # The previous version routed non-HRU params through the HRU selector,
    # which made SWAT+ try to index aqu_dat(hru_id) and crash whenever any
    # HRU id exceeded the (often zero) aquifer count.
    body_rows: List[str] = []
    # First pass: HRU rows (per worker block).
    for sid, params, hrus, change_types in per_worker_blocks:
        if not hrus:
            continue
        hru_strs = " ".join(str(int(h)) for h in hrus)
        n_hru = len(hrus)
        for name, value in params.items():
            obj_typ = _OBJ_TYP.get(name, "hru")
            if obj_typ != "hru":
                continue
            ctype = change_types.get(name, "absval")
            row = (
                f"  {name:<14s} {ctype:<11s} {value:<12.4f} "
                f"{'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} "
                f"{n_hru:>9d} {hru_strs}"
            )
            body_rows.append(row)

    # Second pass: bsn/aqu (global) rows, taken from the FIRST worker in
    # per_worker_blocks (apply_order[0] = outlet). Non-HRU params can only
    # have one value basin-wide, so per-worker values are not meaningful;
    # using the outlet's value is the natural default.
    global_params: Dict[str, Tuple[float, str]] = {}
    if per_worker_blocks:
        _outlet_sid, outlet_params, _h, outlet_ct = per_worker_blocks[0]
        for name, value in outlet_params.items():
            obj_typ = _OBJ_TYP.get(name, "hru")
            if obj_typ == "hru":
                continue
            ctype = outlet_ct.get(name, "absval")
            global_params[name] = (value, ctype)
    for name, (value, ctype) in global_params.items():
        # obj_tot = 0 → SWAT+ applies to all elements of that obj_typ
        # (1 for bsn, sp_ob%aqu for aqu — possibly zero, which is a no-op).
        row = (
            f"  {name:<14s} {ctype:<11s} {value:<12.4f} "
            f"{'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} {'0':>9s} "
            f"{'0':>9s}"
        )
        body_rows.append(row)

    with cal_path.open("w") as f:
        f.write("calibration.cal: written by swatplus_runner.write_calibration_cal_batched\n")
        f.write(f"{len(body_rows)}\n")
        f.write(
            "  cal_parm       chg_typ     chg_val     conds     lyr1      lyr2      "
            "year1     year2     day1      day2      obj_tot   hru_ids\n"
        )
        for row in body_rows:
            f.write(row + "\n")


def compute_exclusive_hru_sets(
    apply_order: List[str],
    hru_by_sid: Dict[str, List[int]],
) -> Dict[str, List[int]]:
    """Return per-sid HRU lists with INNER gauges' HRUs removed from OUTER.

    *apply_order* is the outer-first ordering. For nested basins this yields:

        outer HRUs minus union(every later gauge in apply_order).
        inner HRUs unchanged.

    The result is that every HRU appears in AT MOST ONE block, so the SWAT+
    runtime applies each ``pctchg`` / ``absval`` row exactly once per HRU.
    """
    exclusive: Dict[str, List[int]] = {}
    # Walk in reverse: each later (inner) gauge keeps its full HRU set; we
    # subtract from earlier (outer) gauges any HRU that already belongs to a
    # later gauge.
    later_union: set = set()
    # Build later_union in order so we can subtract correctly.
    for sid in reversed(apply_order):
        hrus = hru_by_sid.get(sid, []) or []
        own = sorted(set(int(h) for h in hrus) - later_union)
        exclusive[sid] = own
        later_union.update(int(h) for h in hrus)
    return exclusive


# ---------------------------------------------------------------------------
# Working-copy management
# ---------------------------------------------------------------------------

# Output files / dirs from a previous SWAT+ run that should NOT be copied
# (they will be regenerated). Skipping these cuts ~210 MB per copy and a lot
# of wall-time when the template lives on a slow external disk.
_SKIP_COPY_PATTERNS = (
    "_day.txt", "_yr.txt", "_aa.txt", "_mon.txt",
    ".out", "_stat.txt", "_change_out.txt",
    "fort.7777", "success.fin",
)
_SKIP_COPY_DIRS = {"SWIFT", "_build"}
_SKIP_EXACT = {"erosion.txt", "checker.out", "diagnostics.out",
               "files_out.out", "area_calc.out", "simulation.out",
               "co2.out", "reservoir_sed.txt",
               "hru_cbn_lyr.txt", "hru_totc.txt", "hru_orgc.txt",
               "basin_carbon_all.txt", "basin_totc.txt"}


def _should_skip(name: str) -> bool:
    if name in _SKIP_EXACT:
        return True
    if name in _SKIP_COPY_DIRS:
        return True
    for pat in _SKIP_COPY_PATTERNS:
        if name.endswith(pat):
            return True
    return False


def _ignore_outputs(_src: str, names: List[str]) -> List[str]:
    return [n for n in names if _should_skip(n)]


_STAGED_CACHES: Dict[str, Path] = {}
import threading
_STAGE_LOCK = threading.Lock()


def _stage_template_local(template_dir: Path, work_root: Path) -> Path:
    """Stage a local cache of the template TxtInOut once.

    The cache lives at `work_root.parent / "_swatplus_template_cache" /
    <hash-of-template-path>`. Subsequent per-run copies can then be made
    local-to-local (very fast) instead of repeatedly reading from the
    (potentially slow) source disk.
    """
    template_key = str(template_dir.resolve())
    cached = _STAGED_CACHES.get(template_key)
    if cached is not None and (cached / ".staged_ok").exists():
        return cached

    with _STAGE_LOCK:
        # Re-check inside the lock in case another thread staged while we waited.
        cached = _STAGED_CACHES.get(template_key)
        if cached is not None and (cached / ".staged_ok").exists():
            return cached

        import hashlib
        digest = hashlib.sha1(template_key.encode("utf-8")).hexdigest()[:12]
        # Use work_root.parent (== scratch_dir root) so all basins under the
        # same scratch root share one cache.
        cache_root = work_root.parent / "_swatplus_template_cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        cache = cache_root / digest
        marker = cache / ".staged_ok"

        if marker.exists():
            _STAGED_CACHES[template_key] = cache
            return cache
        if cache.exists():
            shutil.rmtree(cache)
        cache.mkdir(parents=True, exist_ok=True)
        logger.info("Staging template -> %s (one-time copy from %s)", cache, template_dir)
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


def _make_working_txtinout(
    template_dir: Path,
    work_root: Path,
    run_tag: str,
) -> Path:
    """Create a per-run working copy of TxtInOut.

    First call stages the template into a local cache; subsequent calls
    copy from that local cache (very fast). Skips prior output files
    (*_day.txt, *.out, etc.) to keep the working copy small.
    """
    local_template = _stage_template_local(template_dir, work_root)
    target = work_root / f"TxtInOut_{run_tag}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        str(local_template),
        str(target),
        symlinks=True,
        ignore=_ignore_outputs,
    )
    return target


# Optional fallback cal_parms.cal location, taken from environment.
# Set MADR_SWATPLUS_CAL_PARMS to an absolute path if you want the runner to
# auto-copy a known-good cal_parms.cal into TxtInOut directories that lack
# one. If unset, the runner expects the template to already contain
# cal_parms.cal.
_FALLBACK_CAL_PARMS = (
    Path(os.environ["MADR_SWATPLUS_CAL_PARMS"])
    if os.environ.get("MADR_SWATPLUS_CAL_PARMS")
    else None
)


def ensure_calibration_enabled(txtinout_dir: Path) -> None:
    """Make sure SWAT+ will read calibration.cal.

    - Copy cal_parms.cal into txtinout_dir if missing (from fallback when
      MADR_SWATPLUS_CAL_PARMS is set).
    - Update the `chg` line in file.cio so SWAT+ reads cal_parms.cal +
      calibration.cal. The chg line columns are:
        chg  cal_parms.cal  calibration.cal  ...
    """
    cal_parms = txtinout_dir / "cal_parms.cal"
    if not cal_parms.exists() and _FALLBACK_CAL_PARMS and _FALLBACK_CAL_PARMS.exists():
        shutil.copy2(_FALLBACK_CAL_PARMS, cal_parms)
        logger.info("Copied cal_parms.cal fallback (from MADR_SWATPLUS_CAL_PARMS) into %s", txtinout_dir)

    cio_path = txtinout_dir / "file.cio"
    if not cio_path.exists():
        logger.warning("file.cio not found in %s", txtinout_dir)
        return

    lines = cio_path.read_text().split("\n")
    changed = False
    for i, line in enumerate(lines):
        if line.startswith("chg"):
            # The in_chg derived type has 9 fields, so the chg line must have
            # 10 tokens (name + 9 fields). With list-directed reads Fortran
            # will silently consume the next line's tokens if any are missing,
            # which corrupts the file.cio parser (we've seen this crash
            # soils_init because the soils line gets shifted).
            new_line = (
                "chg               cal_parms.cal     calibration.cal   null              "
                "null              null              null              null              null              null"
            )
            if line != new_line:
                lines[i] = new_line
                changed = True
            break
    if changed:
        cio_path.write_text("\n".join(lines))
        logger.info("Patched chg line in %s/file.cio", txtinout_dir)


# ---------------------------------------------------------------------------
# Output parser (hru_wb_day.txt -> daily Q aggregated per gauge)
# ---------------------------------------------------------------------------

def _load_unit_to_area(subbasin_attrs_file: Path) -> Tuple[Dict[int, int], Dict[int, float]]:
    """Load FID->unit and unit->area_km2 from subbasin_attrs.json.

    The SWAT+ assembler enumerated subbasin_attrs in order, so:
        unit (sub_id) = index + 1
    """
    attrs = json.loads(Path(subbasin_attrs_file).read_text())
    fid_to_unit = {h["FID"]: (i + 1) for i, h in enumerate(attrs)}
    unit_to_area = {(i + 1): float(h["area_km2"]) for i, h in enumerate(attrs)}
    return fid_to_unit, unit_to_area


def parse_hru_wb_day(
    hru_file: Path,
    units_in_gauge: List[int],
    unit_to_area: Dict[int, float],
    start_date: str = "",
    end_date: str = "",
) -> List[Tuple[str, float]]:
    """Return list of (date_str, Q_m3s) summed over the given HRU units.

    hru_wb_day.txt format (per parse_nested_metrics.py):
        line 1: title
        line 2: header (col names incl. yr mon day unit wateryld)
        line 3: units row (mm mm mm ...)
        lines 4+: data
    """
    if not hru_file.exists():
        logger.warning("hru_wb_day.txt not found: %s", hru_file)
        return []

    lines = hru_file.read_text().strip().split("\n")
    if len(lines) < 4:
        return []

    header = lines[1].split()
    try:
        yr_i  = header.index("yr")
        mon_i = header.index("mon")
        day_i = header.index("day")
        unit_i = header.index("unit")
        wyld_i = header.index("wateryld")
    except ValueError as e:
        logger.warning("hru_wb_day header missing required column: %s", e)
        return []

    start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None

    unit_set = set(units_in_gauge)
    daily_vol: Dict[str, float] = {}  # date -> sum of Q_m3 over HRUs

    for line in lines[3:]:
        parts = line.split()
        if len(parts) <= wyld_i:
            continue
        try:
            unit = int(parts[unit_i])
            if unit not in unit_set:
                continue
            yr = int(parts[yr_i])
            mon = int(parts[mon_i])
            day = int(parts[day_i])
            date = datetime(yr, mon, day)
            if start and date < start:
                continue
            if end and date > end:
                continue
            wyld_mm = float(parts[wyld_i])
            area_km2 = unit_to_area.get(unit, 0.0)
            # Volume m3 = wateryld(mm) * area(km2) * 1e3
            vol_m3 = wyld_mm * area_km2 * 1e3
            key = date.strftime("%Y-%m-%d")
            daily_vol[key] = daily_vol.get(key, 0.0) + vol_m3
        except (ValueError, IndexError):
            continue

    # Convert daily volume (m3) to discharge (m3/s)
    result = [(d, v / 86400.0) for d, v in sorted(daily_vol.items())]
    return result


# ---------------------------------------------------------------------------
# Observed-flow parser
# ---------------------------------------------------------------------------

def parse_obs_file(obs_path: Path) -> Dict[str, float]:
    """Parse observed-flow file into {date_str (YYYY-MM-DD): flow}.

    Real format (Huaihe gauge files):
      - Encoding: latin1
      - Line endings: CRLF
      - Tab-separated
      - Columns: stcd, dates, z, Q, name (header on line 1; data from line 2)
      - Q can be -99 or -99.9 for missing values; must filter

    Also tolerates the simpler `date  flow` format used in tests.
    """
    if not obs_path.exists():
        return {}

    # Try the real 5-column format first (latin1, CRLF, tab-separated)
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
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    dt = datetime.strptime(date_str, "%Y/%m/%d")
                flow = float(q_str)
                # Filter -99 / -99.9 sentinel values
                if flow < 0:
                    continue
                out[dt.strftime("%Y-%m-%d")] = flow
            except (ValueError, IndexError):
                continue
        return out

    # Fallback: simple `date  flow` whitespace-separated format
    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            continue
        date_str = parts[0]
        try:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                dt = datetime.strptime(date_str, "%Y/%m/%d")
            flow = float(parts[1])
            if flow < 0:
                continue
            out[dt.strftime("%Y-%m-%d")] = flow
        except (ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------------------
# Performance metrics (NSE / KGE / PBIAS / RMSE / R2)
# ---------------------------------------------------------------------------

def compute_metrics(sim: List[float], obs: List[float]) -> Dict[str, float]:
    n = len(sim)
    if n == 0 or n != len(obs):
        return {"NSE": float("nan"), "KGE": float("nan"), "PBIAS": float("nan"),
                "RMSE": float("nan"), "R2": float("nan")}

    obs_mean = sum(obs) / n
    sim_mean = sum(sim) / n

    obs_var = sum((o - obs_mean) ** 2 for o in obs)
    nse = 1 - sum((o - s) ** 2 for o, s in zip(obs, sim)) / obs_var if obs_var > 0 else float("nan")

    pbias = 100 * sum(o - s for o, s in zip(obs, sim)) / sum(obs) if sum(obs) != 0 else float("nan")

    rmse = math.sqrt(sum((o - s) ** 2 for o, s in zip(obs, sim)) / n)

    sim_std = math.sqrt(sum((s - sim_mean) ** 2 for s in sim) / n)
    obs_std = math.sqrt(sum((o - obs_mean) ** 2 for o in obs) / n)
    if sim_std == 0 or obs_std == 0:
        r = float("nan")
    else:
        r = sum((s - sim_mean) * (o - obs_mean) for s, o in zip(sim, obs)) / (n * sim_std * obs_std)

    if obs_std == 0 or sim_std == 0 or obs_mean == 0 or sim_mean == 0 or math.isnan(r):
        kge = float("nan")
    else:
        beta = sim_mean / obs_mean
        gamma = (sim_std / sim_mean) / (obs_std / obs_mean)
        kge = 1 - math.sqrt((r - 1) ** 2 + (beta - 1) ** 2 + (gamma - 1) ** 2)

    r2 = r * r if not math.isnan(r) else float("nan")

    return {
        "NSE": round(nse, 4) if not math.isnan(nse) else float("nan"),
        "KGE": round(kge, 4) if not math.isnan(kge) else float("nan"),
        "PBIAS": round(pbias, 2) if not math.isnan(pbias) else float("nan"),
        "RMSE": round(rmse, 2),
        "R2": round(r2, 4) if not math.isnan(r2) else float("nan"),
    }


# ---------------------------------------------------------------------------
# One full SWAT+ run
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
    """Execute SWAT+ once for the given config and parameter set.

    Parameters
    ----------
    output_csv_path : optional
        If provided, write a `date, Q_sim_m3s, Q_obs_m3s` CSV covering the
        full simulation period (cfg.start_date .. cfg.end_date). Missing obs
        days are written as empty strings.
    cleanup : bool
        If True (default), the per-run TxtInOut working directory is deleted
        after metrics/CSV are produced. Set False for debugging.
    """
    template = Path(cfg.txtinout_template)
    work_root = Path(cfg.base_output_dir) / f"{cfg.basin_name}_swatplus_cali"
    work_root.mkdir(parents=True, exist_ok=True)

    run_tag = f"run{run_id}_{phase_tag}".replace(" ", "_")
    work_dir = _make_working_txtinout(template, work_root, run_tag)

    # 1a. Ensure SWAT+ will actually apply calibration.cal: copy cal_parms.cal
    #     fallback into the working dir if missing and patch the file.cio chg line.
    ensure_calibration_enabled(work_dir)

    # 1b. Write calibration.cal
    write_calibration_cal(work_dir, params, cfg.change_types)

    # 2. Run binary
    t0 = time.time()
    try:
        proc = subprocess.run(
            [str(cfg.swatplus_exe)],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=7200,
        )
    except subprocess.TimeoutExpired:
        logger.error("SWAT+ timed out for %s run %d", cfg.basin_name, run_id)
        return False, {}
    elapsed = time.time() - t0

    log_text = (
        f"=== SWAT+ run {run_id} ({cfg.basin_name}) ===\n"
        f"exe: {cfg.swatplus_exe}\nwork_dir: {work_dir}\n"
        f"exit_code: {proc.returncode}\nelapsed: {elapsed:.1f}s\n"
        f"\n--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}\n"
    )
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text(log_text)

    if proc.returncode != 0:
        logger.error("SWAT+ exit code %d for %s", proc.returncode, cfg.basin_name)
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        return False, {}

    # 3. Parse simulated Q (aggregate HRU water yield per gauge)
    hru_file = work_dir / "hru_wb_day.txt"
    # Build FID -> unit and unit -> area_km2 from subbasin_attrs.json
    if not getattr(cfg, "subbasin_attrs_file", "") or not Path(cfg.subbasin_attrs_file).exists():
        logger.error("subbasin_attrs_file missing for %s: %s",
                     cfg.basin_name, getattr(cfg, "subbasin_attrs_file", ""))
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        return False, {}
    fid_to_unit, unit_to_area = _load_unit_to_area(Path(cfg.subbasin_attrs_file))
    units_in_gauge = [fid_to_unit[fid] for fid in cfg.hru_fids if fid in fid_to_unit]
    if not units_in_gauge:
        logger.error("No HRU units resolved for %s from fids=%s",
                     cfg.basin_name, cfg.hru_fids)
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        return False, {}

    sim_pairs = parse_hru_wb_day(
        hru_file, units_in_gauge, unit_to_area,
        start_date=cfg.start_date, end_date=cfg.end_date,
    )
    if not sim_pairs:
        logger.warning("No simulated Q parsed for %s (units %s)",
                       cfg.basin_name, units_in_gauge)
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        return False, {}

    sim_by_date = dict(sim_pairs)

    # 4. Parse observed Q and align (for metrics computation)
    obs_by_date = parse_obs_file(Path(cfg.obs_file))
    common = sorted(set(sim_by_date) & set(obs_by_date))
    if not common:
        logger.warning("No overlapping dates between sim and obs for %s",
                       cfg.basin_name)
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        return False, {}
    sim = [sim_by_date[d] for d in common]
    obs = [obs_by_date[d] for d in common]

    # 5. Metrics
    metrics = compute_metrics(sim, obs)
    metrics["n_days"] = len(common)

    # 6. Persist daily discharge time series (full simulation period, obs
    #    aligned by date with empty values for missing days).
    if output_csv_path:
        try:
            _write_discharge_csv(
                Path(output_csv_path),
                sim_by_date,
                obs_by_date,
                cfg.start_date,
                cfg.end_date,
            )
        except Exception as e:
            logger.warning("Failed to write discharge CSV %s: %s",
                           output_csv_path, e)

    # 7. Clean up per-run TxtInOut scratch (template cache is preserved
    #    at work_root.parent / "_swatplus_template_cache/").
    if cleanup and work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)

    return True, metrics


# ---------------------------------------------------------------------------
# Daily-discharge CSV writer
# ---------------------------------------------------------------------------

def _write_discharge_csv(
    csv_path: Path,
    sim_by_date: Dict[str, float],
    obs_by_date: Dict[str, float],
    start_date: str,
    end_date: str,
) -> None:
    """Write `date, Q_sim_m3s, Q_obs_m3s` CSV over [start_date, end_date].

    Missing values are written as empty strings (NaN-equivalent). Mirrors
    the VIC routing `BB.day` convention used elsewhere in the project.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        # Fall back to whatever sim dates we have if config dates are bad
        if not sim_by_date:
            return
        keys = sorted(sim_by_date.keys())
        start = datetime.strptime(keys[0], "%Y-%m-%d")
        end = datetime.strptime(keys[-1], "%Y-%m-%d")

    from datetime import timedelta
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


# ---------------------------------------------------------------------------
# Batched single-run entry point (one SWAT+ call covers all 5 sub-basins)
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
    """Run SWAT+ exactly once with all workers' params applied to disjoint HRU sets.

    Parameters
    ----------
    base_cfg
        Any object that exposes the basin-wide fields used here:
        ``basin_name``, ``txtinout_template``, ``swatplus_exe``,
        ``subbasin_attrs_file``. A :class:`SWATPlusConfig` registered for
        the outlet sub-basin works; callers usually pass it through from
        the executor.
    run_id
        Numeric ID for log / tmpdir naming.
    per_worker_params
        ``{subbasin_id: {param: value}}``.
    worker_specs
        List of :class:`WorkerSpec`. One entry per worker that participates
        in this batched run (gauges' hru_fids + obs_file + simulation
        period).
    apply_order
        Ordered list of subbasin_ids: OUTER first, INNER last. Inner
        gauges' HRU sets are kept intact; outer gauges' HRU sets have
        every inner-gauge HRU removed before writing the calibration.cal
        row, so each HRU receives exactly one calibration row per param.
    phase_tag
        Free-form tag stored in the per-run log.
    log_path
        Path to append a per-run summary log line.
    output_csv_paths
        Optional ``{subbasin_id: csv_path}`` mapping. If provided, a per-day
        ``date,Q_sim_m3s,Q_obs_m3s`` CSV is written for every worker.
    change_types
        Override the default change-type per parameter (forwarded to the
        calibration.cal writer). Falls back to ``base_cfg.change_types``.

    Returns
    -------
    (ok, metrics_by_sid, sim_by_sid, obs_by_sid)
        ``ok`` is True iff the SWAT+ binary succeeded. ``metrics_by_sid`` is
        keyed by subbasin_id, ``sim_by_sid`` / ``obs_by_sid`` hold the daily
        time series (date -> Q m3/s) for every worker.
    """
    ct = dict(getattr(base_cfg, "change_types", {}))
    if change_types:
        ct.update(change_types)

    # Translate per-worker hru_fids (raw geographic FIDs, range 26..233 in
    # this watershed) into SWAT+ HRU unit IDs (1..N) BEFORE writing the
    # calibration.cal HRU selector. The hru_fids stored in the gauge mapping
    # are FIDs from the GIS layer, NOT SWAT+ unit numbers; if we feed them
    # to calibration.cal directly, SWAT+ will index cn2(FID) and crash with
    # an out-of-bounds error whenever any FID > n_hru.
    subbasin_attrs_file = getattr(base_cfg, "subbasin_attrs_file", "")
    if subbasin_attrs_file and Path(subbasin_attrs_file).exists():
        fid_to_unit_cal, _ = _load_unit_to_area(Path(subbasin_attrs_file))
    else:
        fid_to_unit_cal = {}

    # Compute exclusive HRU sets so each HRU is touched by at most one block.
    # Use translated unit IDs throughout so calibration.cal stays in unit space.
    hru_by_sid: Dict[str, List[int]] = {
        s.subbasin_id: [fid_to_unit_cal[fid] for fid in s.hru_fids
                        if fid in fid_to_unit_cal]
        for s in worker_specs
    }
    exclusive_hrus = compute_exclusive_hru_sets(apply_order, hru_by_sid)

    # Build the per-worker block list, preserving apply_order.
    sid_to_spec = {s.subbasin_id: s for s in worker_specs}
    blocks: List[Tuple[str, Dict[str, float], List[int], Dict[str, str]]] = []
    for sid in apply_order:
        if sid not in per_worker_params or sid not in sid_to_spec:
            continue
        hrus = exclusive_hrus.get(sid, [])
        blocks.append((sid, per_worker_params[sid], hrus, ct))

    # ─── Stage TxtInOut ───────────────────────────────────────────────
    template = Path(base_cfg.txtinout_template)
    work_root = Path(base_cfg.base_output_dir) / f"{base_cfg.basin_name}_swatplus_cali_batched"
    work_root.mkdir(parents=True, exist_ok=True)

    run_tag = f"run{run_id}_{phase_tag}".replace(" ", "_")
    work_dir = _make_working_txtinout(template, work_root, run_tag)

    ensure_calibration_enabled(work_dir)
    write_calibration_cal_batched(work_dir, blocks)

    # ─── Run binary ───────────────────────────────────────────────────
    t0 = time.time()
    try:
        proc = subprocess.run(
            [str(base_cfg.swatplus_exe)],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=7200,
        )
    except subprocess.TimeoutExpired:
        logger.error("SWAT+ (batched) timed out for run %d", run_id)
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        nan = _batched_nan_metrics()
        return False, {s.subbasin_id: dict(nan) for s in worker_specs}, {}, {}
    elapsed = time.time() - t0

    log_text = (
        f"=== SWAT+ batched run {run_id} ({phase_tag}) ===\n"
        f"exe: {base_cfg.swatplus_exe}\nwork_dir: {work_dir}\n"
        f"exit_code: {proc.returncode}\nelapsed: {elapsed:.1f}s\n"
        f"workers (apply_order): {apply_order}\n"
        f"\n--- STDOUT(tail) ---\n{proc.stdout[-2000:]}\n"
        f"--- STDERR(tail) ---\n{proc.stderr[-2000:]}\n"
    )
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(log_text)
    except Exception:
        pass

    if proc.returncode != 0:
        logger.error("SWAT+ batched exit code %d", proc.returncode)
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        nan = _batched_nan_metrics()
        return False, {s.subbasin_id: dict(nan) for s in worker_specs}, {}, {}

    # ─── Parse hru_wb_day.txt once, extract per-gauge Q ───────────────
    hru_file = work_dir / "hru_wb_day.txt"
    subbasin_attrs_file = getattr(base_cfg, "subbasin_attrs_file", "")
    if not subbasin_attrs_file or not Path(subbasin_attrs_file).exists():
        logger.error("subbasin_attrs_file missing for batched run: %s", subbasin_attrs_file)
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        nan = _batched_nan_metrics()
        return False, {s.subbasin_id: dict(nan) for s in worker_specs}, {}, {}
    fid_to_unit, unit_to_area = _load_unit_to_area(Path(subbasin_attrs_file))

    metrics_by_sid: Dict[str, Dict[str, float]] = {}
    sim_by_sid: Dict[str, Dict[str, float]] = {}
    obs_by_sid: Dict[str, Dict[str, float]] = {}

    for spec in worker_specs:
        units = [fid_to_unit[fid] for fid in spec.hru_fids if fid in fid_to_unit]
        if not units:
            logger.warning("No HRU units resolved for %s from fids=%s",
                           spec.subbasin_id, spec.hru_fids)
            metrics_by_sid[spec.subbasin_id] = _batched_nan_metrics()
            sim_by_sid[spec.subbasin_id] = {}
            obs_by_sid[spec.subbasin_id] = {}
            continue
        sim_pairs = parse_hru_wb_day(
            hru_file, units, unit_to_area,
            start_date=spec.start_date, end_date=spec.end_date,
        )
        sim_by_date = dict(sim_pairs)
        try:
            obs_by_date = parse_obs_file(Path(spec.obs_file)) if spec.obs_file else {}
        except Exception:
            obs_by_date = {}

        common = sorted(set(sim_by_date) & set(obs_by_date))
        if not common:
            metrics_by_sid[spec.subbasin_id] = _batched_nan_metrics()
        else:
            sim = [sim_by_date[d] for d in common]
            obs = [obs_by_date[d] for d in common]
            m = compute_metrics(sim, obs)
            m["n_days"] = len(common)
            metrics_by_sid[spec.subbasin_id] = m

        sim_by_sid[spec.subbasin_id] = sim_by_date
        obs_by_sid[spec.subbasin_id] = obs_by_date

        if output_csv_paths and output_csv_paths.get(spec.subbasin_id):
            try:
                _write_discharge_csv(
                    Path(output_csv_paths[spec.subbasin_id]),
                    sim_by_date,
                    obs_by_date,
                    spec.start_date,
                    spec.end_date,
                )
            except Exception as e:
                logger.warning("Failed to write discharge CSV for %s: %s",
                               spec.subbasin_id, e)

    # ─── Cleanup per-run TxtInOut ─────────────────────────────────────
    if cleanup and work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)

    return True, metrics_by_sid, sim_by_sid, obs_by_sid


def _batched_nan_metrics() -> Dict[str, float]:
    return {
        "NSE": float("nan"),
        "KGE": float("nan"),
        "PBIAS": float("nan"),
        "RMSE": float("nan"),
        "R2": float("nan"),
        "peak_ratio": float("nan"),
        "lowflow_ratio": float("nan"),
    }
