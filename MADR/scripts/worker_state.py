"""Rule-based parameter proposals from structured Leader instructions.

The normal instruction path returns one phase-constrained candidate; its
exception fallback returns two perturbed candidates. LLM
invocation and candidate evaluation are controlled by the caller."""

import random
import logging
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Default VIC parameter bounds
# ============================================================================

PARAM_BOUNDS = {
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

# Magnitude mapped to a fraction of the parameter range
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
    """Translate structured instructions into parameter candidates.

The normal path returns one candidate; the exception fallback
returns two perturbed candidates."""

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
        """Generate parameters from the supplied center and structured guidance.

Phase locks, bounds, radius and the supplied decay factor constrain the proposal.
The normal path returns one candidate; the exception fallback
returns two candidates. The caller controls their evaluation."""
        self.last_instruction_trace = None
        try:
            adjustments = leader_instruction.get("param_adjustments", [])
            active_keys = self._get_active_keys(phase)

            # Parse guidance as {param: (direction_sign, fraction)}
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

            # Apply the caller-provided decay factor through candidate intensity.
            logger.info("  [AGENT] stagnation=%d", station_stagnation)

            # Single candidate: prescribed step multiplied by decay
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
        """Generate one randomly perturbed escape candidate independently of guidance.

        base_params is the escape-local best; phase defines active parameters.
        perturbation_pct is a fraction of each parameter range. A zero
        n_params_to_perturb selects all active parameters. phase_scale adjusts
        the perturbation amplitude."""
        active_keys = self._get_active_keys(phase)
        scaled_pct = perturbation_pct * phase_scale

        # Select parameters to perturb
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
                # Apply only a small perturbation to unselected active parameters
                noise = random.gauss(0, (hi - lo) * 0.005)
                new_val = base_params[key] + noise
            candidate[key] = max(lo, min(hi, new_val))

        return [candidate]

    # Internal helpers

    def _apply_instruction(
        self,
        base_params: Dict[str, float],
        instructions: Dict[str, Tuple[float, float]],
        active_keys: List[str],
        intensity: float,
        radius: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Generate a range-scaled candidate; the radius argument is not used."""
        candidate = dict(base_params)
        raw_values = dict(base_params)
        sampled_noise = {}

        for key in active_keys:
            if key not in base_params:
                continue
            lo, hi = self.ranges.get(key, (0, 1))
            param_range = hi - lo

            if key in instructions:
                sign, frac = instructions[key]
                step = sign * param_range * frac * intensity
                # Small Gaussian noise avoids exactly symmetric candidates
                noise = random.gauss(0, param_range * 0.01)
                new_val = base_params[key] + step + noise
            else:
                # Apply only a small perturbation when guidance omits a parameter
                noise = random.gauss(0, param_range * 0.005)
                new_val = base_params[key] + noise

            raw_values[key] = new_val
            sampled_noise[key] = noise
            candidate[key] = max(lo, min(hi, new_val))

        self.last_instruction_trace = {"raw_rule_proposal": raw_values, "sampled_noise": sampled_noise,
                                       "effective_instructions": dict(instructions), "intensity": intensity,
                                       "radius_policy": "inactive"}
        return candidate

    def _get_active_keys(self, phase: str) -> List[str]:
        """Return parameters active in the requested calibration phase."""
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
        """Generate a small-perturbation fallback candidate."""
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
