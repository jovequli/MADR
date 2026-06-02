#!/usr/bin/env python3
"""Routing grid utilities for VIC routing parameter calibration.

Provides ESRI-ASCII grid I/O and composite grid generation for per-sub-basin
routing calibration.  Downstream basins inherit upstream basins' calibrated
velocity/diffusivity at geographically matching cells.

Usage:
    from .routing_utils import (
        UpstreamRoutingInfo, generate_uniform_grid, generate_composite_grid,
        find_direc_in_template,
    )
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

ESRI_HEADER_LINES = 6


# ── data classes ────────────────────────────────────────────────────────

@dataclass
class ESRIHeader:
    """Parsed ESRI-ASCII grid header."""
    ncols: int
    nrows: int
    xllcorner: float
    yllcorner: float
    cellsize: float
    nodata_value: float = 0.0


@dataclass
class UpstreamRoutingInfo:
    """Routing calibration state for one upstream sub-basin."""
    direc_path: str      # Path to upstream's *_direc.txt
    velocity: float      # Upstream's calibrated velocity  (m/s)
    diffusivity: float   # Upstream's calibrated diffusivity (m²/s)


# ── I/O helpers ─────────────────────────────────────────────────────────

def read_esri_header(path: str) -> Tuple[ESRIHeader, List[str], List[str]]:
    """Read an ESRI-ASCII grid file.

    Returns (header, raw_header_lines, data_lines).
    """
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    header_lines = lines[:ESRI_HEADER_LINES]
    data_lines = lines[ESRI_HEADER_LINES:]

    kv: Dict[str, str] = {}
    for line in header_lines:
        parts = line.strip().split()
        if len(parts) >= 2:
            kv[parts[0].lower()] = parts[1]

    header = ESRIHeader(
        ncols=int(kv.get("ncols", 0)),
        nrows=int(kv.get("nrows", 0)),
        xllcorner=float(kv.get("xllcorner", 0)),
        yllcorner=float(kv.get("yllcorner", 0)),
        cellsize=float(kv.get("cellsize", 0)),
        nodata_value=float(kv.get("nodata_value", 0)),
    )
    return header, header_lines, data_lines


def find_direc_in_template(rout_template_dir: str) -> str:
    """Find the *_direc.txt file in a routing template directory.

    Returns absolute path, or empty string if not found.
    """
    if not os.path.isdir(rout_template_dir):
        return ""
    for name in sorted(os.listdir(rout_template_dir)):
        if name.endswith("_direc.txt") and not name.startswith("._"):
            return os.path.join(rout_template_dir, name)
    return ""


# ── coordinate helpers ──────────────────────────────────────────────────

def _cell_coord(header: ESRIHeader, col: int, row: int) -> Tuple[int, int]:
    """Convert grid (col, row) to integer-scaled geographic coordinates.

    Uses ×1000 scaling to avoid floating-point comparison issues.
    Row 0 in ESRI-ASCII = northernmost row.
    """
    x = round((header.xllcorner + col * header.cellsize) * 1000)
    y = round((header.yllcorner + (header.nrows - 1 - row) * header.cellsize) * 1000)
    return x, y


def _parse_cell_value(token: str) -> Optional[int]:
    """Parse a cell token to int, returning None if unparseable."""
    try:
        return int(token)
    except ValueError:
        try:
            return int(float(token))
        except ValueError:
            return None


def _build_upstream_mask(
    upstream_infos: List[UpstreamRoutingInfo],
    param_name: str,
) -> Dict[Tuple[int, int], float]:
    """Build a {(x, y) -> value} lookup from all upstream direction files.

    If multiple upstream basins claim the same cell, the last one wins
    (in practice sub-basins should not overlap).
    """
    mask: Dict[Tuple[int, int], float] = {}
    for info in upstream_infos:
        if not info.direc_path or not os.path.exists(info.direc_path):
            continue
        header, _, data_lines = read_esri_header(info.direc_path)
        val = info.velocity if param_name == "velocity" else info.diffusivity

        for row_idx, line in enumerate(data_lines):
            for col_idx, token in enumerate(line.strip().split()):
                cell = _parse_cell_value(token)
                if cell is not None and cell != 0:
                    mask[_cell_coord(header, col_idx, row_idx)] = val
    return mask


# ── grid generation ─────────────────────────────────────────────────────

def generate_uniform_grid(
    direc_path: str, output_path: str, value: float,
) -> None:
    """Generate a routing parameter grid with uniform value.

    Every active (non-zero) cell in the direction file gets *value*;
    zero cells stay 0.  Works for any basin / grid size.
    """
    _, header_lines, data_lines = read_esri_header(direc_path)

    with open(output_path, "w", encoding="utf-8") as f:
        for h in header_lines:
            f.write(h)
        for row in data_lines:
            parts = row.strip().split()
            if not parts:
                f.write(row)
                continue
            out = []
            for p in parts:
                cell = _parse_cell_value(p)
                if cell is None:
                    out.append(p)
                else:
                    out.append(f"{value:.2f}" if cell != 0 else "0")
            f.write(" ".join(out) + "\n")


def generate_composite_grid(
    direc_path: str,
    output_path: str,
    own_value: float,
    upstream_infos: List[UpstreamRoutingInfo],
    param_name: str = "velocity",
) -> None:
    """Generate a composite routing parameter grid for a downstream basin.

    Cells matching an upstream basin's active area get the upstream's
    calibrated value; remaining active cells get *own_value*.

    Falls back to uniform grid if no valid upstream info is provided.

    Args:
        direc_path:  This basin's direction file.
        output_path: Output grid path.
        own_value:   Value for this basin's own cells.
        upstream_infos: Upstream basins' routing info.
        param_name:  "velocity" or "diffusivity".
    """
    upstream_mask = _build_upstream_mask(upstream_infos, param_name)
    if not upstream_mask:
        generate_uniform_grid(direc_path, output_path, own_value)
        return

    header, header_lines, data_lines = read_esri_header(direc_path)

    with open(output_path, "w", encoding="utf-8") as f:
        for h in header_lines:
            f.write(h)
        for row_idx, line in enumerate(data_lines):
            parts = line.strip().split()
            if not parts:
                f.write(line)
                continue
            out = []
            for col_idx, token in enumerate(parts):
                cell = _parse_cell_value(token)
                if cell is None:
                    out.append(token)
                elif cell == 0:
                    out.append("0")
                else:
                    coord = _cell_coord(header, col_idx, row_idx)
                    if coord in upstream_mask:
                        out.append(f"{upstream_mask[coord]:.2f}")
                    else:
                        out.append(f"{own_value:.2f}")
            f.write(" ".join(out) + "\n")
