#!/usr/bin/env python3
"""Parameter validator and auto-fixer for VIC calibration parameters.

Enforces physical constraints, range clamping, precision, and provides
fallback strategies when LLM outputs are invalid.
"""

from __future__ import annotations

import copy
import json
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default parameter specification (can be overridden by SKILL.md spec)
# ---------------------------------------------------------------------------

# VIC+Lohmann routing params (the original 8-param set).
PARAM_KEYS = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3", "velocity", "diffusivity"]

# Soil-only parameter keys (for VIC soil file modification)
SOIL_PARAM_KEYS = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3"]

# Lohmann routing parameter keys
ROUTING_PARAM_KEYS = ["velocity", "diffusivity"]

# CaMa-Flood channel parameter keys (used by the vic_cama_flood combination).
CAMA_PARAM_KEYS = ["Hc", "Hmin", "Wc", "Wmin"]

# SWAT+ calibration parameter keys (used by the swat combination).
# Mirrors the list verified against cal_parms.cal in the SWAT+ port.
SWAT_PARAM_KEYS = [
    "cn2", "esco", "awc", "surlag", "lat_ttime", "canmx",
    "epco", "perco", "alpha", "revap_co", "revap_min", "flo_min", "deep_seep",
    "snofall_tmp", "snomelt_tmp", "snomelt_max", "snomelt_min", "snomelt_lag",
]

# Routing defaults (used when the routing slot is "frozen" in VIC_ONLY).
ROUTING_DEFAULTS = {"velocity": 1.5, "diffusivity": 800.0}


class CalibrationPhase(str, Enum):
    """Calibration phase controlling which parameters are tunable.

    Phase names are shared across model combinations; each executor declares
    its own tunable-keys table for the combination it implements (see
    ``ModelExecutor.tunable_keys_by_phase``). The helpers below are the
    *default* mappings used when a caller needs locked/tunable keys without
    a live executor in hand.
    """
    VIC_ONLY = "VIC_ONLY"                     # Land surface only
    ROUTING_ONLY = "ROUTING_ONLY"             # Lohmann routing (vic_routing combo)
    CAMA_FLOOD_ONLY = "CAMA_FLOOD_ONLY"       # CaMa-Flood routing (vic_cama_flood combo)
    SWATPLUS_HRU = "SWATPLUS_HRU"             # SWAT+ all-params (swat combo single-phase)
    JOINT = "JOINT"                           # All params tunable


def get_tunable_keys(phase: CalibrationPhase) -> List[str]:
    """Default tunable-key set per phase (combination-agnostic).

    Each executor overrides this via its own ``tunable_keys_by_phase`` for
    accuracy; this helper is the safe lowest-common-denominator that does
    not crash when called from code that doesn't have an executor handle.
    """
    if phase == CalibrationPhase.VIC_ONLY:
        return list(SOIL_PARAM_KEYS)
    elif phase == CalibrationPhase.ROUTING_ONLY:
        return list(ROUTING_PARAM_KEYS)
    elif phase == CalibrationPhase.CAMA_FLOOD_ONLY:
        return list(CAMA_PARAM_KEYS)
    elif phase == CalibrationPhase.SWATPLUS_HRU:
        return list(SWAT_PARAM_KEYS)
    else:  # JOINT
        return list(PARAM_KEYS) + list(CAMA_PARAM_KEYS) + list(SWAT_PARAM_KEYS)


def get_locked_keys(phase: CalibrationPhase) -> List[str]:
    """Default locked-key set per phase. See get_tunable_keys for caveats."""
    if phase == CalibrationPhase.VIC_ONLY:
        return list(ROUTING_PARAM_KEYS) + list(CAMA_PARAM_KEYS)
    elif phase == CalibrationPhase.ROUTING_ONLY:
        return list(SOIL_PARAM_KEYS) + list(CAMA_PARAM_KEYS)
    elif phase == CalibrationPhase.CAMA_FLOOD_ONLY:
        return list(SOIL_PARAM_KEYS) + list(ROUTING_PARAM_KEYS)
    elif phase == CalibrationPhase.SWATPLUS_HRU:
        # SWAT+ tunes its own param set; everything VIC/CaMa is locked.
        return list(PARAM_KEYS) + list(CAMA_PARAM_KEYS)
    else:  # JOINT
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
    "binfilt":     (0.001, 1.0),
    "Ds":          (0.001, 1.0),
    "Dsmax":       (1.0,  30.0),
    "Ws":          (0.001, 1.0),
    "soil_d2":     (0.2,   1.5),
    "soil_d3":     (0.5,   2.0),
    "velocity":    (0.5,   5.0),      # m/s, river flow velocity
    "diffusivity": (200.0, 3000.0),   # m²/s, diffusion coefficient
}

# Minimum values for parameters that must not be zero
MIN_NONZERO = {
    "binfilt":     0.001,
    "Ds":          0.001,
    "Ws":          0.001,
    "velocity":    0.5,
    "diffusivity": 200.0,
}

DECIMALS = 3  # soil parameters keep 3 decimal places

# Per-parameter decimal precision overrides
PARAM_DECIMALS = {
    "velocity": 2,
    "diffusivity": 0,
}

# Magnitude mapping: LLM outputs "large"/"medium"/"small" -> fraction of range
MAGNITUDE_MAP: Dict[str, float] = {
    "large":  0.15,
    "medium": 0.08,
    "small":  0.03,
    "none":   0.00,
}


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def normalize_params(
    params: Dict[str, float],
    ranges: Optional[Dict[str, Tuple[float, float]]] = None,
) -> Dict[str, float]:
    """Normalize parameters: clamp to range, enforce constraints, round."""
    rng = ranges or DEFAULT_RANGES
    out: Dict[str, float] = {}

    for k in PARAM_KEYS:
        lo, hi = rng.get(k, DEFAULT_RANGES[k])
        out[k] = clamp(float(params.get(k, (lo + hi) / 2)), lo, hi)

    # CaMa-Flood channel params (vic_cama_flood combination): normalize only
    # when present in the supplied ranges, so vic_routing configs — which carry
    # no CaMa keys in param_ranges — keep bit-identical behaviour.
    for k in CAMA_PARAM_KEYS:
        if k in rng:
            lo, hi = rng[k]
            out[k] = clamp(float(params.get(k, (lo + hi) / 2)), lo, hi)

    # Enforce minimum non-zero values
    for k, min_val in MIN_NONZERO.items():
        out[k] = max(out[k], min_val)

    # Enforce soil_d3 > soil_d2
    if out["soil_d3"] <= out["soil_d2"]:
        lo3, hi3 = rng.get("soil_d3", DEFAULT_RANGES["soil_d3"])
        out["soil_d3"] = clamp(out["soil_d2"] + 0.05, lo3, hi3)
        # If still not valid, try adjusting soil_d2 down
        if out["soil_d3"] <= out["soil_d2"]:
            lo2, _ = rng.get("soil_d2", DEFAULT_RANGES["soil_d2"])
            out["soil_d2"] = clamp(out["soil_d3"] - 0.05, lo2, out["soil_d3"] - 0.001)

    # Round to required precision
    out = {k: round(v, PARAM_DECIMALS.get(k, DECIMALS)) for k, v in out.items()}

    # Final check after rounding
    if out["soil_d3"] <= out["soil_d2"]:
        out["soil_d3"] = round(out["soil_d2"] + 0.05, DECIMALS)

    return out


def apply_adjustments(
    center: Dict[str, float],
    adjustments: List[Dict[str, Any]],
    ranges: Optional[Dict[str, Tuple[float, float]]] = None,
) -> Dict[str, float]:
    """Apply LLM-generated direction+magnitude adjustments to a parameter center.

    Each adjustment: {"param": "binfilt", "direction": "increase", "magnitude": 0.05}
    or: {"param": "binfilt", "direction": "increase", "magnitude": "medium"}

    Returns normalized parameters.
    """
    rng = ranges or DEFAULT_RANGES
    params = dict(center)

    for adj in adjustments:
        # Handle alternative key names (e.g., Gemini uses "parameter" instead of "param")
        param = adj.get("param") or adj.get("parameter") or adj.get("parameter_name") or adj.get("param_name") or adj.get("name") or ""
        if param not in PARAM_KEYS:
            logger.warning("Unknown param in adjustment: %s", param)
            continue

        direction = adj.get("direction", "hold")
        magnitude = adj.get("magnitude", 0.0)

        # Convert string magnitude to numeric
        if isinstance(magnitude, str):
            lo, hi = rng.get(param, DEFAULT_RANGES[param])
            frac = MAGNITUDE_MAP.get(magnitude, 0.0)
            magnitude = frac * (hi - lo)

        magnitude = abs(float(magnitude))

        if direction == "increase":
            params[param] = params.get(param, 0) + magnitude
        elif direction == "decrease":
            params[param] = params.get(param, 0) - magnitude
        # "hold" -> no change

    return normalize_params(params, rng)


def generate_candidates_from_proposals(
    center: Dict[str, float],
    proposals: List[Dict[str, Any]],
    ranges: Optional[Dict[str, Tuple[float, float]]] = None,
) -> List[Dict[str, float]]:
    """Convert a list of LLM worker proposals into concrete parameter sets.

    Each proposal has an "adjustments" list.
    Returns list of normalized parameter dicts.
    """
    candidates = []
    for proposal in proposals:
        adjustments = proposal.get("adjustments", [])
        candidate = apply_adjustments(center, adjustments, ranges)
        candidates.append(candidate)
    return candidates


def generate_contrarian_candidates(
    center: Dict[str, float],
    proposals: List[Dict[str, Any]],
    n_contrarian: int = 3,
    ranges: Optional[Dict[str, Tuple[float, float]]] = None,
    phase: Optional[CalibrationPhase] = None,
) -> List[Dict[str, float]]:
    """Generate contrarian candidates by reversing directions from LLM proposals.

    Takes the LLM's forward proposals and flips increase↔decrease to explore
    the opposite direction. This combats equifinality and helps escape local optima.

    Args:
        center: Current parameter center.
        proposals: LLM-generated proposals (each with "adjustments" list).
        n_contrarian: Number of contrarian candidates to generate.
        ranges: Parameter ranges for clamping.
        phase: Current calibration phase (respects locks).

    Returns list of normalized contrarian parameter dicts.
    """
    if not proposals:
        return []

    locked = set(get_locked_keys(phase)) if phase else set()
    rng = ranges or DEFAULT_RANGES

    # Sort proposals by total magnitude (prefer reversing larger adjustments)
    def _resolve_param(a: Dict) -> str:
        return a.get("param") or a.get("parameter") or a.get("parameter_name") or a.get("param_name") or a.get("name") or ""

    def _total_magnitude(prop: Dict) -> float:
        total = 0.0
        for a in prop.get("adjustments", []):
            if _resolve_param(a) in locked:
                continue
            mag = a.get("magnitude", 0)
            if isinstance(mag, str):
                mag = MAGNITUDE_MAP.get(mag, 0.0)
            try:
                total += abs(float(mag))
            except (ValueError, TypeError):
                pass
        return total

    sorted_proposals = sorted(proposals, key=_total_magnitude, reverse=True)

    contrarians = []
    for i in range(min(n_contrarian, len(sorted_proposals))):
        prop = sorted_proposals[i % len(sorted_proposals)]
        reversed_adjustments = []
        for adj in prop.get("adjustments", []):
            param = _resolve_param(adj)
            direction = adj.get("direction", "hold")
            magnitude = adj.get("magnitude", 0)

            if param in locked:
                reversed_adjustments.append({
                    "param": param, "direction": "hold", "magnitude": 0,
                })
            elif direction == "increase":
                reversed_adjustments.append({
                    "param": param, "direction": "decrease", "magnitude": magnitude,
                })
            elif direction == "decrease":
                reversed_adjustments.append({
                    "param": param, "direction": "increase", "magnitude": magnitude,
                })
            else:
                reversed_adjustments.append({
                    "param": param, "direction": "hold", "magnitude": 0,
                })

        candidate = apply_adjustments(center, reversed_adjustments, rng)
        contrarians.append(candidate)

    return contrarians


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

    def add_fix(self, msg: str):
        self.auto_fixed.append(msg)

    def to_dict(self) -> Dict:
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "auto_fixed": self.auto_fixed,
        }


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


def validate_worker_output(data: Dict[str, Any]) -> ValidationResult:
    """Validate worker LLM output against expected schema."""
    result = ValidationResult()

    if not isinstance(data, dict):
        result.add_error("Worker output is not a dict")
        return result

    # round_id and subbasin_id are context-known, only warn if missing
    for field in ["round_id", "subbasin_id"]:
        if field not in data:
            result.add_warning(f"Missing optional field: {field}")

    # proposals is the only truly critical field
    for field in ["proposals"]:
        if field not in data:
            result.add_error(f"Missing required field: {field}")

    proposals = data.get("proposals", [])
    if not isinstance(proposals, list) or len(proposals) == 0:
        result.add_error("proposals must be a non-empty array")
    else:
        for i, p in enumerate(proposals):
            if "adjustments" not in p:
                result.add_error(f"proposals[{i}] missing adjustments")
            else:
                for adj in p.get("adjustments", []):
                    # Accept both "param" and "parameter" as key names
                    param_name = adj.get("param") or adj.get("parameter") or adj.get("parameter_name") or adj.get("param_name") or adj.get("name")
                    if param_name not in PARAM_KEYS:
                        result.add_warning(f"proposals[{i}] unknown param: {param_name}")
                    if adj.get("direction") not in ("increase", "decrease", "hold"):
                        result.add_warning(f"proposals[{i}] invalid direction: {adj.get('direction')}")
                    mag = adj.get("magnitude")
                    if mag is not None and not isinstance(mag, (int, float, str)):
                        result.add_warning(f"proposals[{i}] magnitude should be number or string")

    return result


def validate_params(
    params: Dict[str, float],
    ranges: Optional[Dict[str, Tuple[float, float]]] = None,
) -> ValidationResult:
    """Validate a concrete parameter set."""
    result = ValidationResult()
    rng = ranges or DEFAULT_RANGES

    for k in PARAM_KEYS:
        if k not in params:
            result.add_error(f"Missing param: {k}")
            continue
        v = params[k]
        lo, hi = rng.get(k, DEFAULT_RANGES[k])
        if v < lo or v > hi:
            result.add_warning(f"{k}={v} out of range [{lo}, {hi}]")
        if k in MIN_NONZERO and v < MIN_NONZERO[k]:
            result.add_warning(f"{k}={v} below minimum {MIN_NONZERO[k]}")

    if "soil_d2" in params and "soil_d3" in params:
        if params["soil_d3"] <= params["soil_d2"]:
            result.add_error(f"soil_d3 ({params['soil_d3']}) must be > soil_d2 ({params['soil_d2']})")

    return result


# ---------------------------------------------------------------------------
# Fallback strategies
# ---------------------------------------------------------------------------

def fallback_rule_based_candidates(
    center: Dict[str, float],
    radius: Dict[str, float],
    n_candidates: int = 6,
    ranges: Optional[Dict[str, Tuple[float, float]]] = None,
    phase: Optional[CalibrationPhase] = None,
) -> List[Dict[str, float]]:
    """Generate candidates using deterministic rule-based strategy.

    Used as fallback when LLM output is invalid or LLM call fails.
    When phase is specified, only perturbs tunable params; locked params stay at center.
    """
    import hashlib

    rng = ranges or DEFAULT_RANGES
    locked = get_locked_keys(phase) if phase else []
    candidates = []

    # Candidate 0: center itself
    candidates.append(normalize_params(center, rng))

    # Remaining candidates: deterministic perturbations
    for i in range(1, n_candidates):
        p = {}
        for k in PARAM_KEYS:
            if k in locked:
                p[k] = center.get(k, 0.5)
            else:
                seed_key = f"fallback-{i}-{k}"
                h = hashlib.sha256(seed_key.encode()).hexdigest()
                u = int(h[:12], 16) / float(16**12 - 1)
                delta = radius.get(k, 0.05) * (2.0 * u - 1.0)
                p[k] = center.get(k, 0.5) + delta
        candidates.append(normalize_params(p, rng))

    return candidates


def safe_apply_leader_guidance(
    center: Dict[str, float],
    radius: Dict[str, float],
    guidance: Dict[str, Any],
    ranges: Optional[Dict[str, Tuple[float, float]]] = None,
    phase: Optional[CalibrationPhase] = None,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Apply leader's guidance to update center and radius, with safety checks.

    When phase is specified, only applies adjustments to tunable params;
    locked params keep their center/radius unchanged.

    Returns (new_center, new_radius).
    """
    rng = ranges or DEFAULT_RANGES
    locked = get_locked_keys(phase) if phase else []
    new_center = dict(center)
    new_radius = dict(radius)

    # Apply param adjustments to center (only tunable params)
    adjustments = guidance.get("param_adjustments", [])
    if adjustments:
        if locked:
            # Filter out adjustments for locked params
            filtered = [
                a for a in adjustments
                if (a.get("param") or a.get("parameter") or a.get("parameter_name") or a.get("param_name") or a.get("name", "")) not in locked
            ]
            new_center = apply_adjustments(center, filtered, rng)
            # Restore locked params to original center
            for k in locked:
                new_center[k] = center[k]
        else:
            new_center = apply_adjustments(center, adjustments, rng)

    # Apply search radius hint
    radius_hint = guidance.get("search_radius_hint", "keep")
    if radius_hint == "shrink":
        factor = 0.85
    elif radius_hint == "expand":
        factor = 1.15
    else:
        factor = 1.0

    for k in PARAM_KEYS:
        lo, hi = rng.get(k, DEFAULT_RANGES[k])
        min_r = (hi - lo) * 0.02
        max_r = (hi - lo) * 0.40
        if k in locked:
            new_radius[k] = radius.get(k, 0.0)  # freeze radius for locked params
        else:
            new_radius[k] = clamp(radius.get(k, 0.05) * factor, min_r, max_r)

    return new_center, new_radius
