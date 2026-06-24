"""VIC + CaMa-Flood executor.

Pipeline per candidate evaluation:
    1. VIC run (delegated to scripts/vic_executor.py)               <- shared with VIC+Routing
    2. VIC fluxes -> NetCDF runoff conversion                       <- _convert_fluxes_to_nc
    3. Write Hc/Hmin/Wc/Wmin into s01-channel_params.sh             <- _write_channel_params
    4. Regenerate rivwth.bin / rivhgt.bin (calc_outclm + calc_rivwth) <- _regenerate_channel_geometry
    5. Run MAIN_cmf with a per-candidate namelist                   <- _run_cama_flood
    6. Read CaMa output at outlet, compute metrics vs observations  <- _read_cama_output + _compute_metrics

Mode = "simulate" returns deterministic stub metrics — used to validate the
calibration loop + per-round file layout without requiring the CaMa-Flood
binary. Mode = "real" invokes the actual binary and external scripts.

`cama_flood_config` in the team JSON supplies the CaMa-Flood paths:

    "cama_flood_config": {
        "cmf_pkg_dir":     "/abs/path/to/cmf_v420_pkg",
        "basin_map":       "bengbu_15min",          // dir name inside map/
        "forcing_dir":     "/abs/path/to/inp/huaihe",
        "forcing_var":     "Runoff",
        "diminfo":         "diminfo_bengbu_025deg.txt",
        "outlet": {"lat": 32.95, "lon": 117.36},
        "obs_file":        "/abs/path/to/bengbu_obs.csv",
        "start_year":      1990,
        "end_year":        1996,
        "manning_river":   0.03,
        "manning_floodplain": 0.10
    }
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..param_validator import SOIL_PARAM_KEYS
from ..vic_executor import VICExecutor, VICConfig
from . import ModelExecutor

logger = logging.getLogger(__name__)

# CaMa-Flood channel parameters this executor calibrates. Default values
# come from the user's s01-channel_params.sh — used when the param is locked
# in the current phase.
CAMA_PARAM_KEYS: List[str] = ["Hc", "Hmin", "Wc", "Wmin"]
CAMA_PARAM_DEFAULTS: Dict[str, float] = {
    "Hc": 0.1, "Hmin": 1.0, "Wc": 2.5, "Wmin": 5.0,
}
# Power-law exponents and offsets stay at CaMa-Flood defaults (we don't tune them).
CAMA_FIXED_PARAMS: Dict[str, float] = {
    "Hp": 0.5, "Ho": 0.0, "Wp": 0.6, "Wo": 0.0,
}

ALL_PARAM_KEYS: List[str] = list(SOIL_PARAM_KEYS) + list(CAMA_PARAM_KEYS)


def _nan_metrics() -> Dict[str, float]:
    return {
        "NSE": float("nan"), "KGE": float("nan"),
        "RMSE": float("nan"), "PBIAS": float("nan"),
        "peak_ratio": float("nan"), "lowflow_ratio": float("nan"),
        "peak_timing_bias_days": float("nan"),
    }


class VICCamaFloodExecutor(ModelExecutor):
    """VIC land-surface + CaMa-Flood routing executor."""

    def __init__(self, mode: str = "simulate"):
        if mode not in ("simulate", "real"):
            raise ValueError(f"Invalid mode: {mode!r}. Must be 'simulate' or 'real'.")
        self.mode = mode
        # VIC is delegated to the existing VICExecutor so VIC behaviour is
        # bit-identical between vic_routing and vic_cama_flood combinations.
        self._vic = VICExecutor(mode=mode)
        # Per-basin CaMa-Flood configuration, populated by `register_basin`.
        # The VIC half lives in self._vic._vic_configs.
        self._cama_cfgs: Dict[str, Dict[str, Any]] = {}
        # When non-empty, ROUTING_ONLY / CAMA_FLOOD_ONLY rounds skip VIC and
        # reuse this cached flux source per basin.
        self._cached_vic_flux_src: Dict[str, Path] = {}

    # ------------------------------------------------------------- run
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
        upstream_cama_info: Optional[List[Any]] = None,
    ) -> Tuple[bool, Dict[str, float]]:
        if self.mode == "simulate":
            return True, _simulate_metrics(subbasin_id, run_id, candidate_id, params)
        return self._run_real(
            subbasin_id, run_id, candidate_id, params, phase_tag, log_path,
            upstream_routing_info, upstream_soil_info, upstream_cama_info,
        )

    # --------------------------------------------------- register_basin
    def register_basin(self, subbasin_id: str, basin_config: Any) -> None:
        """basin_config may be a VICConfig (we register it on the VIC side)
        OR a dict containing both vic + cama_flood sub-sections."""
        if isinstance(basin_config, VICConfig):
            self._vic.register_basin(subbasin_id, basin_config)
            return
        if isinstance(basin_config, dict):
            vic_cfg = basin_config.get("vic")
            cama_cfg = basin_config.get("cama_flood")
            if vic_cfg is not None:
                # Caller pre-built a VICConfig
                self._vic.register_basin(subbasin_id, vic_cfg)
            if cama_cfg is not None:
                self._cama_cfgs[subbasin_id] = dict(cama_cfg)
            return
        raise TypeError(
            f"register_basin: expected VICConfig or dict, got {type(basin_config).__name__}"
        )

    # ----------------------------------------------------- validate_all
    def validate_all(self) -> Dict[str, List[str]]:
        issues = self._vic.validate_all()
        for sid, cfg in self._cama_cfgs.items():
            missing: List[str] = []
            cmf = Path(cfg.get("cmf_pkg_dir", ""))
            if not cmf.is_dir():
                missing.append(f"cmf_pkg_dir not a directory: {cmf}")
            basin_map = cmf / "map" / cfg.get("basin_map", "")
            if not basin_map.is_dir():
                missing.append(f"basin_map not found: {basin_map}")
            if self.mode == "real":
                binary = cmf / "src" / "MAIN_cmf"
                if not binary.is_file():
                    missing.append(f"CaMa-Flood binary missing: {binary}")
            if missing:
                issues.setdefault(sid, []).extend(missing)
        return issues

    # --------------------------------------------- per-basin output dir
    def get_output_dir(self, subbasin_id: str) -> Optional[Path]:
        vic_cfg = self._vic._vic_configs.get(subbasin_id)
        if vic_cfg is not None:
            return Path(vic_cfg.base_output_dir)
        cama = self._cama_cfgs.get(subbasin_id)
        return Path(cama["output_dir"]) if cama and cama.get("output_dir") else None

    # -------------------------------------- per-candidate artifact copy
    def copy_routing_artifacts(
        self,
        subbasin_id: str,
        dst_dir: Path,
        candidate_tag: str = "",
    ) -> bool:
        """Copy this candidate's lightweight CaMa-Flood artifacts (outlet sim
        hydrograph + namelist + log) into dst_dir. The full gridded NetCDF
        outputs are NOT copied — they would bloat disk across 100+ rounds; the
        outlet timeseries is what we retain per round. Simulate mode: nothing."""
        if self.mode != "real":
            return False
        cama = self._cama_cfgs.get(subbasin_id)
        if not cama:
            return False
        src = cama.get("_last_run_dir")  # set by _run_real()
        if not src or not Path(src).exists():
            return False
        src = Path(src)
        target = dst_dir / candidate_tag if candidate_tag else dst_dir
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for name in ("sim_outlet.csv", "input_cmf.nam"):
            p = src / name
            if p.is_file():
                shutil.copy2(str(p), str(target / name))
        # keep only the last year's CaMa log (diagnostics) to stay light
        logs = sorted(src.glob("log_CaMa_*.txt"))
        if logs:
            shutil.copy2(str(logs[-1]), str(target / logs[-1].name))
        return True

    # ------------------------------------------- land-surface caching
    def cache_intermediates_for_routing(
        self,
        centers: Dict[str, Dict[str, float]],
        output_root: Path,
        state: Dict[str, Any],
        worker_cfgs: List[Dict[str, Any]],
    ) -> None:
        """At the VIC_ONLY -> CAMA_FLOOD_ONLY transition, run VIC once per
        basin at its best params and remember the produced flux directory so
        CAMA_FLOOD_ONLY rounds skip VIC."""
        from ..graph_nodes import _build_upstream_soil_info

        cache_root = output_root / "cached_vic_fluxes"
        cache_root.mkdir(parents=True, exist_ok=True)
        print("  >>> Caching VIC fluxes for CAMA_FLOOD_ONLY phase...")

        for w_cfg in worker_cfgs:
            sid = w_cfg["subbasin_id"]
            vic_cfg = self._vic._vic_configs.get(sid)
            if vic_cfg is None:
                print(f"      {sid}: no VIC config, skipping")
                continue
            best_params = dict(centers.get(sid, {}))
            if not best_params:
                print(f"      {sid}: no best params, skipping")
                continue

            # Run VIC once at best params (flux-only — no Lohmann routing) and
            # cache the raw vic_result dir so CAMA_FLOOD_ONLY rounds skip VIC.
            vic_result_dir = self._run_vic_fluxes(
                sid, 99999, 1,
                {k: v for k, v in best_params.items() if k in SOIL_PARAM_KEYS},
                "cache_vic_flux_for_cama", "",
                _build_upstream_soil_info(sid, state, worker_cfgs),
            )
            if not vic_result_dir or not Path(vic_result_dir).exists():
                print(f"      {sid}: VIC cache run failed")
                continue

            dst = cache_root / sid
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(str(vic_result_dir), str(dst))
            self._cached_vic_flux_src[sid] = dst
            print(f"      {sid}: cached -> {dst}")

    def clear_intermediate_cache(self) -> None:
        self._cached_vic_flux_src.clear()
        for sid, vic_cfg in self._vic._vic_configs.items():
            vic_cfg.vic_flux_source = ""
        print("  >>> Cleared VIC flux cache for JOINT phase")

    # ------------------------------------------------- phase / param meta
    @property
    def tunable_keys_by_phase(self) -> Dict[str, Set[str]]:
        return {
            "VIC_ONLY":          set(SOIL_PARAM_KEYS),
            "CAMA_FLOOD_ONLY":   set(CAMA_PARAM_KEYS),
            "JOINT":             set(ALL_PARAM_KEYS),
        }

    @property
    def all_param_keys(self) -> List[str]:
        return list(ALL_PARAM_KEYS)

    # ===================================================================
    # Real-mode internals
    # ===================================================================

    def _run_real(
        self,
        subbasin_id: str,
        run_id: int,
        candidate_id: int,
        params: Dict[str, float],
        phase_tag: str,
        log_path: str,
        upstream_routing_info: Optional[List[Any]],
        upstream_soil_info: Optional[List[Any]],
        upstream_cama_info: Optional[List[Any]] = None,
    ) -> Tuple[bool, Dict[str, float]]:
        cama = self._cama_cfgs.get(subbasin_id)
        if not cama:
            logger.error("No CaMa config for basin %s", subbasin_id)
            return False, _nan_metrics()

        cmf_pkg = Path(cama["cmf_pkg_dir"])
        basin_map = cmf_pkg / "map" / cama["basin_map"]
        run_tag = phase_tag or f"r{run_id}_c{candidate_id}"
        # Fixed per-basin work dir, reset each run. The lightweight artifacts
        # (sim_outlet.csv etc.) are copied out per round by copy_routing_artifacts
        # before the next run resets this; reusing one dir per basin keeps
        # cmf_pkg/out from accumulating GBs of gridded NetCDF over a long run.
        # Different basins use different sids, so the 6 parallel runs don't clash.
        exp_dir = cmf_pkg / "out" / subbasin_id
        if exp_dir.exists():
            shutil.rmtree(exp_dir)
        exp_dir.mkdir(parents=True, exist_ok=True)
        cama["_last_run_dir"] = str(exp_dir)

        # reuse_runoff (testing): VIC is unchanged, so skip VIC + conversion and
        # use the pre-existing per-year runoff NetCDFs in forcing_dir directly.
        # Only CaMa-Flood re-runs. (Calibration leaves this off so VIC re-runs.)
        if cama.get("reuse_runoff"):
            nc_dir = Path(cama["forcing_dir"])
            prefix = cama.get("forcing_prefix", "runoff")
            syear = int(cama.get("start_year", 1990)); eyear = int(cama.get("end_year", 1996))
            missing = [y for y in range(syear, eyear + 1)
                       if not (nc_dir / f"{prefix}_{y}.nc").is_file()]
            if missing:
                logger.error("[%s] reuse_runoff: missing runoff NCs for years %s in %s",
                             subbasin_id, missing, nc_dir)
                return False, _nan_metrics()
            logger.info("[%s] reuse_runoff: using existing runoff in %s (VIC skipped)",
                        subbasin_id, nc_dir)
        else:
            # 1. VIC (flux-only — CaMa-Flood replaces Lohmann, so no routing template
            #    is required) or reuse cached raw fluxes for CAMA_FLOOD_ONLY rounds.
            if subbasin_id in self._cached_vic_flux_src:
                vic_result_dir = str(self._cached_vic_flux_src[subbasin_id])
                logger.info("[%s] using cached VIC fluxes (%s)", subbasin_id, vic_result_dir)
            else:
                vic_result_dir = self._run_vic_fluxes(
                    subbasin_id, run_id, candidate_id,
                    {k: v for k, v in params.items() if k in SOIL_PARAM_KEYS},
                    run_tag, log_path, upstream_soil_info,
                )
                if not vic_result_dir:
                    logger.error("[%s] VIC run failed", subbasin_id)
                    return False, _nan_metrics()

            # 2. VIC fluxes -> NetCDF runoff on the basin's local 0.25-deg grid
            try:
                nc_dir = self._convert_vic_result_to_nc(subbasin_id, vic_result_dir, cama)
            except Exception as e:  # noqa: BLE001 — propagate as failed run, not crash
                logger.error("[%s] flux->NetCDF conversion failed: %s", subbasin_id, e)
                return False, _nan_metrics()

        # 3. Write candidate's channel params
        try:
            self._write_channel_params(basin_map, params)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] channel-param write failed: %s", subbasin_id, e)
            return False, _nan_metrics()

        # 4. Rebuild rivwth / rivhgt / rivman from the new coefficients
        try:
            self._regenerate_channel_geometry(basin_map)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] channel-geometry rebuild failed: %s", subbasin_id, e)
            return False, _nan_metrics()

        # 4b. Per-reach lock: pin upstream-calibrated channel geometry onto the
        #     upstream reach within this (downstream) basin's grid.
        if upstream_cama_info:
            try:
                self._apply_upstream_channel_lock(basin_map, cama, upstream_cama_info)
            except Exception as e:  # noqa: BLE001
                logger.error("[%s] upstream channel-lock failed: %s", subbasin_id, e)
                return False, _nan_metrics()

        # 5. Run MAIN_cmf
        try:
            self._run_cama_flood(cmf_pkg, basin_map, nc_dir, exp_dir, cama)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] MAIN_cmf invocation failed: %s", subbasin_id, e)
            return False, _nan_metrics()

        # 6. Read output, compute metrics, save the outlet sim hydrograph
        try:
            sim = self._read_cama_output(exp_dir, cama)
            obs = self._read_observations(cama)
            self._save_sim_hydrograph(exp_dir, cama, sim, obs)
            return True, self._compute_metrics(sim, obs)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] metric computation failed: %s", subbasin_id, e)
            return False, _nan_metrics()

    # ---- VIC flux-only run (no Lohmann routing) -----------------------
    def _run_vic_fluxes(
        self,
        subbasin_id: str,
        run_id: int,
        candidate_id: int,
        soil_params: Dict[str, float],
        phase_tag: str,
        log_path: str,
        upstream_soil_info: Optional[List[Any]],
    ) -> Optional[str]:
        """Run VIC only (skip Lohmann routing) and return the raw vic_result dir.

        The vic_cama_flood combination needs VIC grid fluxes but must not require
        a Lohmann routing template (these basins don't ship one). Delegates to
        the additive vic_runner.run_vic_flux_only(); the VIC half stays
        bit-identical with the vic_routing path (same write_soil_params /
        composite upstream-soil writing).
        """
        from .. import vic_runner

        vic_cfg = self._vic._vic_configs.get(subbasin_id)
        if vic_cfg is None:
            logger.error("[%s] no VIC config registered", subbasin_id)
            return None
        rcfg = vic_runner.RuntimeConfig(
            basin_name=vic_cfg.basin_name,
            base_output_dir=vic_cfg.base_output_dir,
            soil_base=vic_cfg.soil_base,
            global_base=vic_cfg.global_base,
            obs_file=vic_cfg.obs_file,
            rout_template_dir=vic_cfg.rout_template_dir,
            vic_exe=vic_cfg.vic_exe,
            rout_exe=vic_cfg.rout_exe,
            start_year=int(vic_cfg.start_year),
            end_year=int(vic_cfg.end_year),
            flux_prefix=vic_cfg.flux_prefix,
        )
        log = log_path or f"/tmp/vic_{subbasin_id}_{run_id}_{candidate_id}.log"
        ok, vic_result = vic_runner.run_vic_flux_only(
            rcfg, run_id, soil_params, phase_tag, log,
            upstream_soil_info=upstream_soil_info,
        )
        return vic_result if ok else None

    # ---- VIC raw fluxes -> CaMa runoff NetCDF (inline) ----------------
    def _convert_vic_result_to_nc(
        self, subbasin_id: str, vic_result_dir: str, cama: Dict[str, Any],
    ) -> Path:
        """Convert raw VIC ASCII flux files (RESULT_DIR) into annual CaMa-Flood
        runoff NetCDFs on the basin's local 0.25-deg grid.

        Logic mirrors vic_cama_pkg/scripts/run_basin_vic_cama.sh step 2 (proven
        working): runoff = OUT_RUNOFF + OUT_BASEFLOW [mm/day]; grid + edges come
        from the basin diminfo; one `<forcing_prefix>_YYYY.nc` per year is
        written into forcing_dir, variable named `forcing_var`.
        """
        import glob
        import re
        import numpy as np
        import pandas as pd
        import netCDF4 as nc

        RES = 0.25
        basin_map = Path(cama["cmf_pkg_dir"]) / "map" / cama["basin_map"]
        diminfo = basin_map / cama["diminfo"]
        forcing_dir = Path(cama["forcing_dir"])
        forcing_dir.mkdir(parents=True, exist_ok=True)
        prefix = cama.get("forcing_prefix", f"{subbasin_id}_runoff_1d")
        var = cama.get("forcing_var", "Runoff")

        lines = diminfo.read_text().split("\n")
        nXX = int(lines[0].split()[0])
        nYY = int(lines[1].split()[0])
        nums = [float(L.split()[0]) for L in lines
                if L.strip() and L.strip().split()[0].replace('.', '').replace('-', '').isdigit()]
        west, east, north, south = nums[-4], nums[-3], nums[-2], nums[-1]
        nlon = int(round((east - west) / RES))
        nlat = int(round((north - south) / RES))
        if (nlon, nlat) != (nXX, nYY):
            raise RuntimeError(f"[{subbasin_id}] grid mismatch {nlon}x{nlat} vs diminfo {nXX}x{nYY}")
        lons = west + (np.arange(nlon) + 0.5) * RES
        lats = north - (np.arange(nlat) + 0.5) * RES  # north -> south

        files = sorted(p for p in glob.glob(os.path.join(vic_result_dir, "*fluxes_*.txt"))
                       if not os.path.basename(p).startswith("._"))
        if not files:
            raise FileNotFoundError(f"no VIC flux files (*fluxes_*.txt) in {vic_result_dir}")

        # header detection: find the YEAR line + runoff/baseflow column indices
        header_skip = 0
        ridx = bidx = None
        with open(files[0]) as f:
            for i, ln in enumerate(f):
                if ln.startswith("#"):
                    continue
                if ln.strip().startswith("YEAR"):
                    cols = ln.strip().split()
                    ridx = next(j for j, c in enumerate(cols) if "OUT_RUNOFF" in c)
                    bidx = next(j for j, c in enumerate(cols) if "OUT_BASEFLOW" in c)
                    header_skip = i + 1
                    break
        if ridx is None or bidx is None:
            raise RuntimeError(f"[{subbasin_id}] OUT_RUNOFF/OUT_BASEFLOW not found in VIC header")

        pat = re.compile(r"fluxes_(-?\d+\.\d+)_(-?\d+\.\d+)\.txt$")
        cells = []
        for p in files:
            m = pat.search(os.path.basename(p))
            if not m:
                continue
            lat, lon = float(m.group(1)), float(m.group(2))
            li = int(round((north - lat) / RES - 0.5))
            xi = int(round((lon - west) / RES - 0.5))
            if 0 <= li < nlat and 0 <= xi < nlon:
                cells.append((li, xi, p))
        logger.info("[%s] converting %d/%d flux cells -> runoff NC", subbasin_id, len(cells), len(files))

        syear = int(cama.get("start_year", 2001))
        eyear = int(cama.get("end_year", 2024))
        for year in range(syear, eyear + 1):
            ndays = 366 if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0 else 365
            R = np.full((ndays, nlat, nlon), -9999.0, dtype=np.float32)
            for li, xi, path in cells:
                try:
                    df = pd.read_csv(path, sep=r'\s+', skiprows=header_skip, header=None, engine='c')
                    yr = df[df.iloc[:, 0] == year]
                    if not len(yr):
                        continue
                    r = (yr.iloc[:, ridx].values + yr.iloc[:, bidx].values).astype(np.float32)
                    n = min(len(r), ndays)
                    R[:n, li, xi] = r[:n]
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] convert %s: %s", subbasin_id, os.path.basename(path), e)
            op = forcing_dir / f"{prefix}_{year}.nc"
            with nc.Dataset(op, "w", format="NETCDF4") as d:
                d.createDimension("time", ndays)
                d.createDimension("lat", nlat)
                d.createDimension("lon", nlon)
                tv = d.createVariable("time", "i4", ("time",))
                tv.units = f"days since {year}-01-01 00:00:00"
                tv.calendar = "standard"
                tv[:] = np.arange(ndays)
                la = d.createVariable("lat", "f4", ("lat",))
                la.units = "degrees_north"
                la[:] = lats
                lo = d.createVariable("lon", "f4", ("lon",))
                lo.units = "degrees_east"
                lo[:] = lons
                rv = d.createVariable(var, "f4", ("time", "lat", "lon"),
                                      zlib=True, complevel=4, fill_value=-9999.0)
                rv.units = "mm/day"
                rv.long_name = "VIC surface+baseflow runoff"
                rv[:] = R
        return forcing_dir

    # ---- VIC -> NetCDF (legacy converter_script path; kept for back-compat) ----
    def _convert_fluxes_to_nc(self, subbasin_id: str, cama: Dict[str, Any]) -> Path:
        """Convert VIC flux text files to CaMa-Flood NetCDF runoff.

        Real deployments should point ``cama_flood_config.converter_script`` at
        the user's `process_data_windows_ymd.py` (or equivalent). If that field
        is missing AND a `forcing_dir` already holds NetCDFs covering the
        calibration period, we trust those and skip conversion — useful for
        smoke-testing CaMa-Flood param tuning in isolation.
        """
        forcing_dir = Path(cama["forcing_dir"])
        forcing_dir.mkdir(parents=True, exist_ok=True)

        converter = cama.get("converter_script")
        if not converter:
            # Pre-converted NetCDFs available — use as-is.
            ncs = sorted(forcing_dir.glob("*.nc"))
            if not ncs:
                raise FileNotFoundError(
                    f"No NetCDFs in {forcing_dir} and no converter_script "
                    f"configured. Set cama_flood_config.converter_script or "
                    f"pre-populate the forcing_dir."
                )
            return forcing_dir

        # Invoke the configured converter. Contract: it writes annual NetCDFs
        # into forcing_dir; we don't dictate its CLI shape — pass through the
        # converter_args list from config verbatim.
        cmd = [str(converter)] + list(cama.get("converter_args", []))
        logger.info("[%s] running converter: %s", subbasin_id, " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise RuntimeError(
                f"converter exited {r.returncode}:\nstdout={r.stdout[-2000:]}\nstderr={r.stderr[-2000:]}"
            )
        return forcing_dir

    # ---- channel params write -----------------------------------------
    @staticmethod
    def _write_channel_params(basin_map: Path, params: Dict[str, float]) -> None:
        """Rewrite ``src_param/s01-channel_params.sh`` with the candidate's
        4 channel coefficients; keep the rest of the file (which runs
        calc_outclm + calc_rivwth) intact."""
        sh = basin_map / "src_param" / "s01-channel_params.sh"
        if not sh.is_file():
            raise FileNotFoundError(f"channel params script not found: {sh}")

        hc   = float(params.get("Hc",   CAMA_PARAM_DEFAULTS["Hc"]))
        hmin = float(params.get("Hmin", CAMA_PARAM_DEFAULTS["Hmin"]))
        wc   = float(params.get("Wc",   CAMA_PARAM_DEFAULTS["Wc"]))
        wmin = float(params.get("Wmin", CAMA_PARAM_DEFAULTS["Wmin"]))
        hp = CAMA_FIXED_PARAMS["Hp"]; ho = CAMA_FIXED_PARAMS["Ho"]
        wp = CAMA_FIXED_PARAMS["Wp"]; wo = CAMA_FIXED_PARAMS["Wo"]

        # Surgical rewrite of just the four `XX=...` lines, preserving the
        # rest of the shell script (paths, calc_outclm/calc_rivwth calls,
        # rivman.bin write, etc.).
        text = sh.read_text(encoding="utf-8")
        replacements = {
            "HC":   f"HC={hc:.4f}",
            "HP":   f"HP={hp:.4f}",
            "HO":   f"HO={ho:.4f}",
            "HMIN": f"HMIN={hmin:.4f}",
            "WC":   f"WC={wc:.4f}",
            "WP":   f"WP={wp:.4f}",
            "WO":   f"WO={wo:.4f}",
            "WMIN": f"WMIN={wmin:.4f}",
        }
        lines = []
        seen: Set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            # Only touch top-level shell-variable assignments like ``HC=0.1``.
            for var, replacement in replacements.items():
                # Match patterns ``VAR=...`` and ``VAR=...; VAR2=...``.
                if stripped.startswith(f"{var}="):
                    line = replacement
                    seen.add(var)
                    break
                # Inline form: "HC=0.1; HP=0.50; HO=0.00; HMIN=1.0"
                if f"{var}=" in line and ";" in line:
                    parts = [p.strip() for p in line.split(";")]
                    new_parts = []
                    for p in parts:
                        repl = None
                        for v, r in replacements.items():
                            if p.startswith(f"{v}="):
                                repl = r
                                seen.add(v)
                                break
                        new_parts.append(repl if repl else p)
                    line = "; ".join(new_parts)
                    break
            lines.append(line)
        missing = sorted(set(replacements) - seen)
        if missing:
            logger.warning("channel-params: did not find %s in %s; "
                           "values may stay at defaults", missing, sh)
        sh.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- rebuild rivwth/rivhgt/rivman after channel params change ----
    @staticmethod
    def _regenerate_channel_geometry(basin_map: Path) -> None:
        sh = basin_map / "src_param" / "s01-channel_params.sh"
        logger.info("running %s", sh)
        r = subprocess.run(
            ["sh", sh.name],
            cwd=str(sh.parent),
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"s01-channel_params.sh exited {r.returncode}:\n"
                f"stdout={r.stdout[-2000:]}\nstderr={r.stderr[-2000:]}"
            )

    # ---- per-reach upstream channel-param lock (nested basins) --------
    @staticmethod
    def _grid_geom(diminfo: Path) -> Tuple[float, float, int, int]:
        """Return (west, north, nXX, nYY) for a basin map from its diminfo."""
        lines = Path(diminfo).read_text().split("\n")
        nXX = int(lines[0].split()[0])
        nYY = int(lines[1].split()[0])
        nums = [float(L.split()[0]) for L in lines
                if L.strip() and L.strip().split()[0].replace('.', '').replace('-', '').isdigit()]
        west, _east, north, _south = nums[-4], nums[-3], nums[-2], nums[-1]
        return west, north, nXX, nYY

    def _apply_upstream_channel_lock(
        self, down_basin_map: Path, down_cama: Dict[str, Any],
        upstream_cama_info: List[Dict[str, Any]],
    ) -> None:
        """Splice each upstream basin's locked channel geometry onto the upstream
        reach within the downstream grid.

        For every upstream basin: regenerate its rivwth/rivhgt/rivman with its
        locked Hc/Hmin/Wc/Wmin (on its own map), then copy those values into the
        downstream grid at matching lat/lon cells (river cells only). The
        downstream's own (incremental) cells keep the candidate's geometry.
        """
        import numpy as np

        RES = 0.25
        names = ("rivwth", "rivhgt", "rivman", "rivwth_gwdlr")
        dn_w, dn_n, dn_nx, dn_ny = self._grid_geom(down_basin_map / down_cama["diminfo"])
        dn = {nm: np.fromfile(down_basin_map / f"{nm}.bin", dtype="<f4").reshape(dn_ny, dn_nx)
              for nm in names}

        n_spliced = 0
        for info in upstream_cama_info:
            up_cama = info["cama_flood_config"]
            up_params = info["params"]
            up_map = Path(up_cama["cmf_pkg_dir"]) / "map" / up_cama["basin_map"]
            # Regenerate upstream geometry at its locked params, then read it.
            self._write_channel_params(up_map, up_params)
            self._regenerate_channel_geometry(up_map)
            up_w, up_n, up_nx, up_ny = self._grid_geom(up_map / up_cama["diminfo"])
            up = {nm: np.fromfile(up_map / f"{nm}.bin", dtype="<f4").reshape(up_ny, up_nx)
                  for nm in names}
            up_wth = up["rivwth"]
            for iy_u in range(up_ny):
                lat = up_n - (iy_u + 0.5) * RES
                iy_d = int(round((dn_n - lat) / RES - 0.5))
                if not (0 <= iy_d < dn_ny):
                    continue
                for ix_u in range(up_nx):
                    if up_wth[iy_u, ix_u] <= 0:  # valid river cells only
                        continue
                    lon = up_w + (ix_u + 0.5) * RES
                    ix_d = int(round((lon - dn_w) / RES - 0.5))
                    if not (0 <= ix_d < dn_nx):
                        continue
                    for nm in names:
                        dn[nm][iy_d, ix_d] = up[nm][iy_u, ix_u]
                    n_spliced += 1

        for nm in names:
            dn[nm].tofile(down_basin_map / f"{nm}.bin")
        logger.info("per-reach channel lock: spliced %d upstream cells into %s",
                    n_spliced, down_basin_map.name)

    # ---- run CaMa-Flood ----------------------------------------------
    def _run_cama_flood(
        self,
        cmf_pkg: Path,
        basin_map: Path,
        nc_dir: Path,
        exp_dir: Path,
        cama: Dict[str, Any],
    ) -> None:
        """Run MAIN_cmf year-by-year with restart chaining.

        CaMa-Flood's CDF forcing opens a SINGLE NetCDF per run (no %Y
        expansion), so we loop annually over the per-year runoff files the
        converter produced and chain restarts (year 1 cold-starts; each later
        year reads the restart written by the previous one). This mirrors the
        proven vic_cama_pkg/scripts/run_basin_vic_cama.sh and addresses the
        README's glue-point #1.
        """
        binary = cmf_pkg / "src" / "MAIN_cmf"
        if not binary.is_file():
            raise FileNotFoundError(f"MAIN_cmf not found at {binary}")

        # Link binary into exp_dir; namelist must be cwd to MAIN_cmf.
        local_binary = exp_dir / "MAIN_cmf"
        if local_binary.exists() or local_binary.is_symlink():
            local_binary.unlink()
        os.symlink(str(binary), str(local_binary))

        diminfo = basin_map / cama.get("diminfo", "")
        if not diminfo.is_file():
            raise FileNotFoundError(f"diminfo not found at {diminfo}")

        syear = int(cama.get("start_year", 1990))
        eyear = int(cama.get("end_year", 1996))
        prefix = cama.get("forcing_prefix", "runoff")

        env = os.environ.copy()
        env.setdefault("OMP_NUM_THREADS", str(cama.get("omp_threads", 4)))

        for year in range(syear, eyear + 1):
            rof = nc_dir / f"{prefix}_{year}.nc"
            if not rof.is_file():
                raise FileNotFoundError(f"runoff forcing not found: {rof}")
            nml = self._build_namelist(basin_map, diminfo, rof, year, cama,
                                       first=(year == syear))
            (exp_dir / "input_cmf.nam").write_text(nml, encoding="utf-8")
            log = exp_dir / f"log_CaMa_{year}.txt"
            with log.open("w") as logf:
                r = subprocess.run(
                    ["./MAIN_cmf"], cwd=str(exp_dir), env=env,
                    stdout=logf, stderr=subprocess.STDOUT, check=False,
                )
            if r.returncode != 0:
                raise RuntimeError(f"MAIN_cmf year {year} exited {r.returncode} — see {log}")

    @staticmethod
    def _build_namelist(
        basin_map: Path,
        diminfo: Path,
        rof: Path,
        year: int,
        cama: Dict[str, Any],
        first: bool,
    ) -> str:
        """One-year namelist (proven run_basin form): LPTHOUT off, NetCDF output,
        per-year forcing, restart chaining."""
        forcing_var = cama.get("forcing_var", "Runoff")
        inpmat = basin_map / f"inpmat_{cama['basin_map'].replace('_15min', '_025deg')}.bin"
        if first:
            lrestart = ".FALSE."
            creststo = "''"
        else:
            lrestart = ".TRUE."
            creststo = f"'./restart{year}010100.nc'"

        return f"""&NRUNVER
  LADPSTP=.TRUE.
  LPTHOUT=.FALSE.
  LRESTART={lrestart}
/
&NDIMTIME
  CDIMINFO='{diminfo}'
  DT=86400
  IFRQ_INP=24
/
&NPARAM
  PMANRIV={cama.get("manning_river", 0.03)}D0
  PMANFLD={cama.get("manning_floodplain", 0.10)}D0
  PDSTMTH=10000.D0
  PCADP=0.7
/
&NSIMTIME
  SYEAR={year}
  SMON=1
  SDAY=1
  SHOUR=0
  EYEAR={year + 1}
  EMON=1
  EDAY=1
  EHOUR=0
/
&NMAP
  LMAPCDF=.FALSE.
  CNEXTXY='{basin_map}/nextxy.bin'
  CGRAREA='{basin_map}/ctmare.bin'
  CELEVTN='{basin_map}/elevtn.bin'
  CNXTDST='{basin_map}/nxtdst.bin'
  CRIVLEN='{basin_map}/rivlen.bin'
  CFLDHGT='{basin_map}/fldhgt.bin'
  CRIVWTH='{basin_map}/rivwth_gwdlr.bin'
  CRIVHGT='{basin_map}/rivhgt.bin'
  CRIVMAN='{basin_map}/rivman.bin'
/
&NRESTART
  CRESTSTO={creststo}
  CRESTDIR='./'
  CVNREST='restart'
  LRESTCDF=.TRUE.
  IFRQ_RST=0
/
&NFORCE
  LINPCDF=.TRUE.
  LINTERP=.TRUE.
  CINPMAT='{inpmat}'
  CROFCDF='{rof}'
  CVNROF='{forcing_var}'
  SYEARIN={year}
  SMONIN=1
  SDAYIN=1
  SHOURIN=0
/
&NOUTPUT
  COUTDIR='./'
  CVARSOUT='outflw'
  COUTTAG='{year}'
  LOUTCDF=.TRUE.
  NDLEVEL=0
  IFRQ_OUT=24
/
&NBOUND
/
&NDAMOUT
/
&NLEVEE
/
"""

    # ---- CaMa output + metrics ----------------------------------------
    def _read_cama_output(self, exp_dir: Path, cama: Dict[str, Any]) -> List[float]:
        """Read daily streamflow at the outlet cell from the per-year NetCDF
        outputs (`o_outflw<YYYY>.nc`, LOUTCDF=.TRUE.), concatenated in order.

        Files are float32 grids shaped [time, nY, nX]; the outlet is the
        0-indexed (iy=row, ix=col) cell from the basin's station_info.
        """
        try:
            import numpy as np
            import netCDF4 as nc
        except ImportError:
            raise RuntimeError("numpy + netCDF4 required to read CaMa-Flood output") from None

        # Evaluate from eval_start_year onward (skip the 2000 spin-up year).
        import re
        eval_start = int(cama.get("eval_start_year", cama.get("start_year", 0)))
        files = []
        for f in sorted(exp_dir.glob("o_outflw*.nc")):
            m = re.search(r"o_outflw(\d{4})", f.name)
            if m and int(m.group(1)) < eval_start:
                continue  # spin-up year — exclude from evaluation
            files.append(f)
        if not files:
            raise FileNotFoundError(f"no o_outflw*.nc (>= {eval_start}) in {exp_dir}")

        outlet = cama.get("outlet", {})
        ix = int(outlet["ix"])  # 0-indexed cell column (lon)
        iy = int(outlet["iy"])  # 0-indexed cell row (lat, from north)

        series: List[float] = []
        for f in files:
            d = nc.Dataset(str(f))
            vname = "outflw" if "outflw" in d.variables else \
                next(k for k in d.variables if "outflw" in k.lower())
            arr = np.array(d.variables[vname][:])  # (time, nY, nX)
            d.close()
            series.extend(float(v) for v in arr[:, iy, ix])
        return series

    @staticmethod
    def _read_grid_size(diminfo: Path) -> Tuple[int, int]:
        with diminfo.open() as f:
            nXX = int(f.readline().split()[0])
            nYY = int(f.readline().split()[0])
        return nXX, nYY

    def _read_observations(self, cama: Dict[str, Any]) -> List[float]:
        """Load daily observed streamflow over the calibration window."""
        try:
            import pandas as pd
        except ImportError:
            raise RuntimeError("pandas required to load observation CSV") from None
        obs_file = Path(cama["obs_file"])
        df = pd.read_csv(obs_file)
        # Slice to the evaluation window [eval_start_year, end_year] (spin-up
        # year 2000 excluded). Obs CSV is a continuous daily series so it aligns
        # positionally with the sim read over the same years.
        eval_start = int(cama.get("eval_start_year", cama.get("start_year", 0)))
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df[(df["date"].dt.year >= eval_start) &
                    (df["date"].dt.year <= cama["end_year"])]
        q_col = "Q" if "Q" in df.columns else df.columns[-1]
        return df[q_col].astype(float).tolist()

    @staticmethod
    def _save_sim_hydrograph(exp_dir: Path, cama: Dict[str, Any],
                             sim: List[float], obs: List[float]) -> None:
        """Write the outlet daily simulated (and observed) discharge for this
        round to exp_dir/sim_outlet.csv over the evaluation window. This is the
        per-round simulated flow we retain; copy_routing_artifacts copies it
        into each round's results dir."""
        try:
            import pandas as pd
        except ImportError:
            return
        eval_start = int(cama.get("eval_start_year", cama.get("start_year", 2001)))
        eyear = int(cama.get("end_year", eval_start))
        idx = pd.date_range(f"{eval_start}-01-01", f"{eyear}-12-31", freq="D")
        n = min(len(idx), len(sim))
        df = pd.DataFrame({"date": idx[:n].strftime("%Y-%m-%d"),
                           "Q_sim": [round(float(v), 4) for v in sim[:n]]})
        if obs:
            m = min(n, len(obs))
            qobs = [None] * n
            for i in range(m):
                qobs[i] = round(float(obs[i]), 4)
            df["Q_obs"] = qobs
        df.to_csv(exp_dir / "sim_outlet.csv", index=False)

    @staticmethod
    def _compute_metrics(sim: List[float], obs: List[float]) -> Dict[str, float]:
        try:
            import numpy as np
        except ImportError:
            raise RuntimeError("numpy required to compute metrics") from None

        n = min(len(sim), len(obs))
        if n == 0:
            return _nan_metrics()
        s = np.asarray(sim[:n], dtype="float64")
        o = np.asarray(obs[:n], dtype="float64")
        mask = np.isfinite(s) & np.isfinite(o)
        s, o = s[mask], o[mask]
        if s.size == 0:
            return _nan_metrics()

        obs_mean = float(o.mean())
        nse = 1.0 - float(((s - o) ** 2).sum()) / max(((o - obs_mean) ** 2).sum(), 1e-9)
        rmse = float(((s - o) ** 2).mean() ** 0.5)
        pbias = 100.0 * float((s.sum() - o.sum()) / max(o.sum(), 1e-9))

        peak_obs = float(np.percentile(o, 95))
        peak_sim = float(np.percentile(s, 95))
        peak_ratio = peak_sim / max(peak_obs, 1e-9)

        low_obs = float(np.percentile(o, 10))
        low_sim = float(np.percentile(s, 10))
        lowflow_ratio = low_sim / max(low_obs, 1e-9)

        return {
            "NSE": nse,
            "KGE": _kge(s, o),
            "RMSE": rmse,
            "PBIAS": pbias,
            "peak_ratio": peak_ratio,
            "lowflow_ratio": lowflow_ratio,
            "peak_timing_bias_days": 0.0,  # could be extracted from peak-day alignment
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _kge(s, o):
    try:
        import numpy as np
        cc = float(np.corrcoef(s, o)[0, 1]) if s.size > 1 else 0.0
        alpha = float(s.std() / max(o.std(), 1e-9))
        beta = float(s.mean() / max(o.mean(), 1e-9))
        return 1.0 - float(((cc - 1) ** 2 + (alpha - 1) ** 2 + (beta - 1) ** 2) ** 0.5)
    except Exception:
        return float("nan")


def _simulate_metrics(
    subbasin_id: str, run_id: int, candidate_id: int, params: Dict[str, float],
) -> Dict[str, float]:
    """Deterministic NSE-like metrics for simulate mode.

    The function rewards parameters near a fixed-point per-basin "optimum"
    and adds small basin-specific offsets so multi-basin runs are interesting.
    Output range: NSE roughly in [-0.5, 0.95].
    """
    # Per-basin synthetic optimum.
    base_optima = {
        # VIC
        "binfilt": 0.20, "Ds": 0.005, "Dsmax": 8.0, "Ws": 0.7,
        "soil_d2": 0.5, "soil_d3": 1.4,
        # CaMa
        "Hc": 0.15, "Hmin": 1.0, "Wc": 2.5, "Wmin": 5.0,
    }
    # Slight basin-specific shifts.
    sid_seed = sum(ord(c) for c in subbasin_id) % 17
    shift = (sid_seed - 8) * 0.01
    target = {k: v * (1 + shift) for k, v in base_optima.items()}

    # Distance from target, normalised by parameter range.
    ranges = {
        "binfilt": (0.001, 0.4), "Ds": (1e-4, 1.0), "Dsmax": (1.0, 30.0),
        "Ws": (0.5, 1.0), "soil_d2": (0.1, 2.0), "soil_d3": (0.5, 2.0),
        "Hc": (0.01, 1.0), "Hmin": (0.5, 2.0), "Wc": (0.1, 4.0), "Wmin": (1.0, 10.0),
    }
    dist_sq = 0.0
    n = 0
    for k, v in params.items():
        lo, hi = ranges.get(k, (0.0, 1.0))
        if k not in target:
            continue
        norm = (v - target[k]) / max(hi - lo, 1e-9)
        dist_sq += norm * norm
        n += 1
    if n == 0:
        return _nan_metrics()
    rms_dist = math.sqrt(dist_sq / n)

    # NSE: 0.95 at target, falls off as distance grows.
    nse = 0.95 - 4.0 * rms_dist
    nse = max(-0.5, min(0.95, nse))

    pbias = 20.0 * (rms_dist - 0.5)  # roughly -10% to +10%
    peak_ratio = 1.0 + 0.5 * rms_dist
    lowflow_ratio = max(0.1, 1.0 + 0.3 * (rms_dist - 0.5))
    return {
        "NSE": round(nse, 4),
        "KGE": round(nse * 0.9, 4),
        "RMSE": round(50.0 * rms_dist, 3),
        "PBIAS": round(pbias, 2),
        "peak_ratio": round(peak_ratio, 3),
        "lowflow_ratio": round(lowflow_ratio, 3),
        "peak_timing_bias_days": round((sid_seed - 8) * 0.2, 1),
    }
