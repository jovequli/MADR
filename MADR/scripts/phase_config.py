#!/usr/bin/env python3
"""Read phase schedules from JSON and determine transitions.

Only the ordered phase pipeline format is accepted."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .graph_state import PhaseSpec
from .baseline_control import validate_pipeline

def load_phase_pipeline(team_config: Dict[str, Any]) -> List[PhaseSpec]:
    """Load the ordered phase pipeline from the team config."""
    phase_cfg = team_config.get("phase_config", {})
    if team_config.get("phase_nse_thresholds") or phase_cfg.get("phase_nse_thresholds"):
        raise ValueError("NSE threshold stopping is not implemented in this execution profile")

    # Explicit pipeline list
    if "pipeline" in phase_cfg:
        pipeline = []
        for entry in phase_cfg["pipeline"]:
            spec: PhaseSpec = {
                "name": entry["name"],
                "max_rounds": int(entry.get("max_rounds", 30)),
                "mc_rounds": int(entry.get("mc_rounds", 0)),
                "mc_candidates": int(entry.get("mc_candidates", 5)),
                "patience_rounds": entry.get("patience_rounds"),
                "patience_min_delta": entry.get("patience_min_delta", 0.0),
            }
            pipeline.append(spec)
        if pipeline and pipeline[0]['name'] == 'ROUTING_ONLY' and team_config.get('initial_exploration', {}).get('enabled'):
            raise ValueError("Soil exploration requires a pre-routing phase; cannot explore into a frozen routing cache")
        return validate_pipeline(pipeline)

    raise ValueError("phase_config.pipeline is required")


def get_total_max_rounds(pipeline: List[PhaseSpec]) -> int:
    """Sum of max_rounds across all phases."""
    return sum(p.get("max_rounds", 0) for p in pipeline)


def check_phase_transition(
    pipeline: List[PhaseSpec],
    current_phase_idx: int,
    rounds_in_current_phase: int,
    rounds_without_improvement: int = 0,
) -> Tuple[bool, Optional[int], str]:
    """Check if current phase should transition to the next.

    Returns:
        (should_transition, next_phase_idx, reason)
    """
    if current_phase_idx >= len(pipeline):
        return False, None, ""

    current = pipeline[current_phase_idx]
    max_rounds = current.get("max_rounds", 999)

    patience = current.get("patience_rounds")
    plateau = (patience is not None and rounds_without_improvement >= patience
               and rounds_in_current_phase > current.get("mc_rounds", 0))
    if rounds_in_current_phase >= max_rounds or plateau:
        next_idx = current_phase_idx + 1
        if next_idx < len(pipeline):
            reason = (f"max_rounds={max_rounds} reached for "
                      f"{current['name']}")
            return True, next_idx, (f"patience={patience} reached" if plateau else reason)
        else:
            # Last phase exhausted — signal completion
            return True, None, (f"patience={patience} reached" if plateau else "all phases completed")

    return False, None, ""


def should_use_multi_candidate(
    pipeline: List[PhaseSpec],
    current_phase_idx: int,
    rounds_in_current_phase: int,
) -> Tuple[bool, int]:
    """Check if current round should use multi-candidate mode.

    Returns (use_multi, n_candidates).
    """
    if current_phase_idx >= len(pipeline):
        return False, 0

    phase = pipeline[current_phase_idx]
    mc_rounds = phase.get("mc_rounds", 0)
    mc_candidates = phase.get("mc_candidates", 5)

    if mc_rounds > 0 and rounds_in_current_phase <= mc_rounds:
        return True, mc_candidates

    return False, 0
