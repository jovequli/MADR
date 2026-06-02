"""
Worker Executor — translates Leader instructions into candidate parameter sets.

Centralized architecture: Leader makes decisions, Worker executes.
This module converts Leader's structured instructions (which param, direction, magnitude)
into candidate parameter values.

No diagnostics (handled by worker_diagnostics).
No autonomous decisions (decision authority stays with Leader).
No LLM calls (whether to call LLM is controlled by orchestrator).

Usage:
    from worker_state import WorkerExecutor

    executor = WorkerExecutor(param_ranges=ranges)

    candidates = executor.generate_candidates(
        base_params=best_params,          # search center (historical best)
        leader_instruction=instruction,    # Leader's structured instruction
        phase="vic_only",
    )
    # candidates: [Dict]  — exactly 1 set
"""

import math
import random
import logging
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Parameter search ranges (consistent with VIC model)
# ============================================================================

PARAM_BOUNDS = {
    "binfilt":     (0.001, 0.4),
    "Ds":          (0.0001, 1.0),
    "Dsmax":       (1.0, 30.0),
    "Ws":          (0.5, 1.0),
    "soil_d2":     (0.2, 1.5),
    "soil_d3":     (0.5, 2.0),
    "velocity":    (0.5, 3.0),
    "diffusivity": (200.0, 4000.0),
}

# magnitude → fraction of parameter range
MAGNITUDE_FRACTION = {
    "small":  0.10,
    "medium": 0.22,
    "large":  0.40,
    "none":   0.0,
}

VIC_KEYS = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3"]
ROUTING_KEYS = ["velocity", "diffusivity"]


# ============================================================================
# WorkerExecutor
# ============================================================================

class WorkerExecutor:
    """
    Worker parameter executor.

    Receives Leader's structured instructions, generates 1 candidate parameter set
    following the Leader's direction and magnitude.
    """

    def __init__(
        self,
        param_ranges: Optional[Dict[str, Tuple[float, float]]] = None,
    ):
        self.ranges = param_ranges or dict(PARAM_BOUNDS)

    def generate_candidates(
        self,
        base_params: Dict[str, float],
        leader_instruction: Dict[str, Any],
        phase: str = "joint",
        radius: Optional[Dict[str, float]] = None,
        escape_mode: bool = False,
        decay_factor: float = 1.0,
        station_stagnation: int = 0,
    ) -> List[Dict[str, float]]:
        """
        Translate Leader instruction into 1 candidate parameter set.

        Parameters
        ----------
        base_params : dict
            Search center (typically historical best params from ledger)
        leader_instruction : dict
            Leader's structured instruction
        phase : str
            Calibration phase, determines which params are tunable
        radius : dict, optional
            Current search radius (used for scaling if provided)
        escape_mode : bool
            Global escape flag (kept for compatibility)
        decay_factor : float
            Decay factor
        station_stagnation : int
            Consecutive rounds without improvement for this station (logging only)

        Returns
        -------
        List[Dict[str, float]]
            1 candidate parameter set
        """
        try:
            adjustments = leader_instruction.get("param_adjustments", [])
            active_keys = self._get_active_keys(phase)

            # Parse Leader instruction into {param: (direction_sign, fraction)}
            instructions = {}
            for adj in adjustments:
                param = adj.get("param", adj.get("parameter", adj.get("parameter_name", adj.get("param_name", adj.get("name", "")))))
                direction = adj.get("direction", "hold")
                magnitude = adj.get("magnitude", "none")

                if param not in active_keys:
                    continue
                if direction == "hold" or magnitude in ("none", "0", 0):
                    continue

                sign = 1.0 if direction == "increase" else -1.0
                frac = MAGNITUDE_FRACTION.get(str(magnitude).lower(), 0.1)
                instructions[param] = (sign, frac)

            # cali5.2_LW: decay_factor unused (always 1.0), kept for compat
            logger.info("  [AGENT] stagnation=%d", station_stagnation)

            # Single candidate: apply instruction step × decay
            candidate = self._apply_instruction(
                base_params, instructions, active_keys,
                intensity=1.0 * decay_factor, radius=radius
            )

            return [candidate]

        except Exception as e:
            logger.error(f"WorkerExecutor.generate_candidates error: {e}")
            return self._fallback_candidates(base_params, phase)

    def generate_escape_candidates(
        self,
        base_params: Dict[str, float],
        phase: str = "joint",
        perturbation_pct: float = 0.10,
        n_params_to_perturb: int = 0,
        phase_scale: float = 1.0,
    ) -> List[Dict[str, float]]:
        """
        Escape mode candidate generation (random perturbation, independent of Leader).

        Parameters
        ----------
        base_params : dict
            escape_best params (best within escape)
        phase : str
            Calibration phase
        perturbation_pct : float
            Perturbation magnitude as fraction of param range (e.g., 0.10=10%)
        n_params_to_perturb : int
            Number of params to randomly perturb. 0=all active params
        phase_scale : float
            Phase scaling factor (VIC=1.0, ROUTING=0.4, JOINT=0.15)

        Returns
        -------
        List[Dict[str, float]]
            1 candidate parameter set
        """
        active_keys = self._get_active_keys(phase)
        scaled_pct = perturbation_pct * phase_scale

        # Select params to perturb
        if n_params_to_perturb > 0 and n_params_to_perturb < len(active_keys):
            perturb_keys = random.sample(active_keys, n_params_to_perturb)
        else:
            perturb_keys = list(active_keys)

        logger.info("  [ESCAPE] pct=%.1f%% (raw=%.0f%% × scale=%.2f), "
                     "perturb %d/%d params: %s",
                     scaled_pct * 100, perturbation_pct * 100, phase_scale,
                     len(perturb_keys), len(active_keys), perturb_keys)

        candidate = dict(base_params)
        for key in active_keys:
            if key not in base_params:
                continue
            lo, hi = self.ranges.get(key, (0, 1))
            if key in perturb_keys:
                r = (hi - lo) * scaled_pct
                new_val = base_params[key] + random.uniform(-r, r)
            else:
                # Unselected params: tiny perturbation for stability
                noise = random.gauss(0, (hi - lo) * 0.005)
                new_val = base_params[key] + noise
            candidate[key] = max(lo, min(hi, new_val))

        return [candidate]

    # ── Internal methods ──────────────────────────────────────────────

    def _apply_instruction(
        self,
        base_params: Dict[str, float],
        instructions: Dict[str, Tuple[float, float]],
        active_keys: List[str],
        intensity: float,
        radius: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Generate one candidate set from instructions."""
        candidate = dict(base_params)

        for key in active_keys:
            if key not in base_params:
                continue
            lo, hi = self.ranges.get(key, (0, 1))
            param_range = hi - lo

            if key in instructions:
                sign, frac = instructions[key]
                step = sign * param_range * frac * intensity
                # Small Gaussian noise (prevent symmetric candidates)
                noise = random.gauss(0, param_range * 0.01)
                new_val = base_params[key] + step + noise
            else:
                # Params not in Leader instruction: tiny perturbation (stability)
                noise = random.gauss(0, param_range * 0.005)
                new_val = base_params[key] + noise

            candidate[key] = max(lo, min(hi, new_val))

        return candidate

    def _get_active_keys(self, phase: str) -> List[str]:
        """Filter active params by calibration phase."""
        if phase == "vic_only":
            return [k for k in VIC_KEYS if k in self.ranges]
        elif phase == "routing_only":
            return [k for k in ROUTING_KEYS if k in self.ranges]
        else:
            return [k for k in VIC_KEYS + ROUTING_KEYS if k in self.ranges]

    def _fallback_candidates(
        self,
        base_params: Dict[str, float],
        phase: str,
    ) -> List[Dict[str, float]]:
        """Safe fallback: tiny perturbation."""
        active_keys = self._get_active_keys(phase)
        candidates = []
        for _ in range(2):
            c = dict(base_params)
            for key in active_keys:
                if key not in base_params:
                    continue
                lo, hi = self.ranges.get(key, (0, 1))
                noise = random.gauss(0, (hi - lo) * 0.03)
                c[key] = max(lo, min(hi, base_params[key] + noise))
            candidates.append(c)
        return candidates
