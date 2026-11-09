#!/usr/bin/env python3
"""Parameter validator and auto-fixer for VIC calibration parameters.

Enforces physical constraints, range clamping, and precision.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Default parameter specification
# ---------------------------------------------------------------------------

PARAM_KEYS = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3", "velocity", "diffusivity"]

# Soil-only parameter keys (for VIC soil file modification)
SOIL_PARAM_KEYS = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3"]

# Routing parameter keys
ROUTING_PARAM_KEYS = ["velocity", "diffusivity"]


class CalibrationPhase(str, Enum):
    """Calibration phase controlling which parameters are tunable."""
    VIC_ONLY = "VIC_ONLY"           # Only soil params tunable
    ROUTING_ONLY = "ROUTING_ONLY"   # Only routing params tunable
    JOINT = "JOINT"                 # All params tunable


def get_tunable_keys(phase: CalibrationPhase) -> List[str]:
    """Return parameter keys that are tunable in the given phase."""
    if phase == CalibrationPhase.VIC_ONLY:
        return list(SOIL_PARAM_KEYS)
    elif phase == CalibrationPhase.ROUTING_ONLY:
        return list(ROUTING_PARAM_KEYS)
    else:
        return list(PARAM_KEYS)


def get_locked_keys(phase: CalibrationPhase) -> List[str]:
    """Return parameter keys that are LOCKED in the given phase."""
    if phase == CalibrationPhase.VIC_ONLY:
        return list(ROUTING_PARAM_KEYS)
    elif phase == CalibrationPhase.ROUTING_ONLY:
        return list(SOIL_PARAM_KEYS)
    else:
        return []


def enforce_phase_locks(
    candidates: List[Dict[str, float]],
    phase: CalibrationPhase,
    frozen_center: Dict[str, float],
) -> List[Dict[str, float]]:
    """Enforce phase-based parameter locking on candidate sets.

    For locked params, overwrite candidate values with the frozen center value.
    This is the hard safety net — even if the LLM ignores prompt instructions.
    """
    locked = get_locked_keys(phase)
    if not locked:
        return candidates
    result = []
    for cand in candidates:
        c = dict(cand)
        for k in locked:
            if k in frozen_center:
                c[k] = frozen_center[k]
        result.append(c)
    return result


DEFAULT_RANGES: Dict[str, Tuple[float, float]] = {
    # Default bounds for the shared VIC/routing parameters.
    # Execution uses validated configured ranges on every proposal path.
    "binfilt":     (0.001, 0.4),
    "Ds":          (0.001, 1.0),
    "Dsmax":       (1.0, 30.0),
    "Ws":          (0.5, 1.0),
    "soil_d2":     (0.1, 2.0),
    "soil_d3":     (0.1, 2.0),
    "velocity":    (0.5, 3.0),
    "diffusivity": (200.0, 4000.0),
}


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def clamp(v, lo, hi):
    from .execution_protocol import _finite
    return max(_finite(lo), min(_finite(hi), _finite(v)))


def normalize_params(params, ranges=None):
    from .execution_protocol import normalize, EFFECTIVE_BOUNDS
    bounds = ranges or EFFECTIVE_BOUNDS
    # Fill missing values with range midpoints; reject nonfinite values.
    complete = {k: params.get(k, sum(bounds.get(k, EFFECTIVE_BOUNDS[k])) / 2) for k in PARAM_KEYS}
    return normalize(complete, bounds)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationResult:
    """Result of validating LLM output."""

    def __init__(self):
        self.is_valid = True
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.auto_fixed: List[str] = []

    def add_error(self, msg: str):
        self.is_valid = False
        self.errors.append(msg)

    def add_warning(self, msg: str):
        self.warnings.append(msg)


def validate_leader_output(data: Dict[str, Any]) -> ValidationResult:
    """Validate leader LLM output against expected schema."""
    result = ValidationResult()

    if not isinstance(data, dict):
        result.add_error("Leader output is not a dict")
        return result

    # round_id is context-known; only warn if missing
    if "round_id" not in data:
        result.add_warning("Missing optional field: round_id")

    # These are the truly required fields
    for field in ["overall_assessment", "stop_or_continue", "worker_guidance"]:
        if field not in data:
            result.add_error(f"Missing required field: {field}")

    if data.get("stop_or_continue") not in ("continue", "stop"):
        result.add_warning(f"Invalid stop_or_continue value: {data.get('stop_or_continue')}")

    guidance = data.get("worker_guidance", [])
    if not isinstance(guidance, list):
        result.add_error("worker_guidance must be an array")
    else:
        for i, g in enumerate(guidance):
            # subbasin_id is helpful but not strictly required; we can match by other fields
            id_keys = ("subbasin_id", "basin_id", "basin", "worker_name", "name", "worker_id")
            if not any(k in g for k in id_keys):
                result.add_warning(f"worker_guidance[{i}] missing subbasin identifier")
            # param_adjustments may use alternative names or be free-text instructions
            adj_keys = ("param_adjustments", "adjustments", "parameter_adjustments",
                        "parameter_updates", "parameter_adjustment")
            adj_list = None
            for ak in adj_keys:
                if ak in g and isinstance(g[ak], list):
                    adj_list = g[ak]
                    break
            # Fallback: find any list field containing dicts with "direction" key
            if adj_list is None:
                for key, val in g.items():
                    if isinstance(val, list) and val and isinstance(val[0], dict) and "direction" in val[0]:
                        adj_list = val
                        break
            if adj_list is None:
                # Not an error — some LLMs use free-text instructions instead
                if "instructions" not in g and "focus" not in g:
                    result.add_warning(f"worker_guidance[{i}] missing param_adjustments")
            else:
                for adj in adj_list:
                    param_name = adj.get("param") or adj.get("parameter") or adj.get("parameter_name") or adj.get("param_name") or adj.get("name")
                    if param_name not in PARAM_KEYS:
                        result.add_warning(f"worker_guidance[{i}] unknown param: {param_name}")
                    if adj.get("direction") not in ("increase", "decrease", "hold"):
                        result.add_warning(f"worker_guidance[{i}] invalid direction: {adj.get('direction')}")

    return result
