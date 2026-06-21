#!/usr/bin/env python3
"""JSON-driven phase transition engine for SADR calibration.

Supports two config formats:
1. New pipeline format:
   {"pipeline": [{"name": "VIC_ONLY", "max_rounds": 60, ...}, ...]}
2. Legacy format:
   {"max_rounds_per_phase": {"VIC_ONLY": 60, "ROUTING_ONLY": 25, "JOINT": 16}}

Adding a new phase only requires editing the JSON — no code changes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .graph_state import PhaseSpec

logger = logging.getLogger(__name__)

# Default pipeline when no config is provided
DEFAULT_PIPELINE: List[PhaseSpec] = [
    {"name": "VIC_ONLY", "max_rounds": 60, "mc_rounds": 10, "mc_candidates": 5},
    {"name": "ROUTING_ONLY", "max_rounds": 25, "mc_rounds": 5, "mc_candidates": 5},
    {"name": "JOINT", "max_rounds": 16, "mc_rounds": 0, "mc_candidates": 0},
]


def load_phase_pipeline(team_config: Dict[str, Any]) -> List[PhaseSpec]:
    """Load phase pipeline from team config.

    Supports both new ``pipeline`` format and legacy ``max_rounds_per_phase``.
    """
    phase_cfg = team_config.get("phase_config", {})

    # ── New format: explicit pipeline list ──
    if "pipeline" in phase_cfg:
        pipeline = []
        for entry in phase_cfg["pipeline"]:
            spec: PhaseSpec = {
                "name": entry["name"],
                "max_rounds": int(entry.get("max_rounds", 30)),
                "mc_rounds": int(entry.get("mc_rounds", 0)),
                "mc_candidates": int(entry.get("mc_candidates", 5)),
                "patience_rounds": int(entry.get("patience_rounds", 999)),
            }
            pipeline.append(spec)
        return pipeline

    # ── Legacy format: max_rounds_per_phase dict ──
    max_per_phase = phase_cfg.get("max_rounds_per_phase", {})
    if max_per_phase:
        # Determine phase order from config or use default
        phase_order = phase_cfg.get("phase_order", ["VIC_ONLY", "ROUTING_ONLY", "JOINT"])

        # Multi-candidate config (legacy location)
        mc_cfg = team_config.get("multi_candidate", {})
        mc_vic = int(mc_cfg.get("vic_rounds", 0))
        mc_rout = int(mc_cfg.get("rout_rounds", 0))
        mc_n = int(mc_cfg.get("n_candidates", 5))

        pipeline = []
        for name in phase_order:
            if name in max_per_phase:
                mc_rounds = 0
                if name == "VIC_ONLY":
                    mc_rounds = mc_vic
                elif name == "ROUTING_ONLY":
                    mc_rounds = mc_rout
                pipeline.append({
                    "name": name,
                    "max_rounds": int(max_per_phase[name]),
                    "mc_rounds": mc_rounds,
                    "mc_candidates": mc_n if mc_rounds > 0 else 0,
                })
        return pipeline

    # ── Fallback: use default pipeline ──
    return list(DEFAULT_PIPELINE)


def get_total_max_rounds(pipeline: List[PhaseSpec]) -> int:
    """Sum of max_rounds across all phases."""
    return sum(p.get("max_rounds", 0) for p in pipeline)


def get_phase_by_index(pipeline: List[PhaseSpec], idx: int) -> Optional[PhaseSpec]:
    """Get phase spec by index, or None if out of bounds."""
    if 0 <= idx < len(pipeline):
        return pipeline[idx]
    return None


def check_phase_transition(
    pipeline: List[PhaseSpec],
    current_phase_idx: int,
    rounds_in_current_phase: int,
    rounds_since_improvement: int = 0,
) -> Tuple[bool, Optional[int], str]:
    """Check if current phase should transition to the next.

    Two trigger paths:
      1. **Patience-based:** if `rounds_since_improvement >= patience_rounds`
         (per-phase config), transition early. This is the dynamic
         convergence rule.
      2. **Hard cap:** if `rounds_in_current_phase >= max_rounds`, transition
         regardless. Safety upper bound.

    Returns:
        (should_transition, next_phase_idx, reason)

        `next_phase_idx is None` means there is no next phase (we are
        finishing the last one). The caller should schedule one final
        force-best round and then exit.
    """
    if current_phase_idx >= len(pipeline):
        return False, None, ""

    current = pipeline[current_phase_idx]
    max_rounds = current.get("max_rounds", 999)
    patience = current.get("patience_rounds", 999)
    name = current["name"]

    # 1. Patience-based termination
    if patience < 999 and rounds_since_improvement >= patience:
        next_idx = current_phase_idx + 1
        reason = (f"patience={patience} rounds without best-NSE improvement "
                  f"in {name} (round {rounds_in_current_phase}/{max_rounds})")
        if next_idx < len(pipeline):
            return True, next_idx, reason
        return True, None, reason

    # 2. Hard max_rounds cap
    if rounds_in_current_phase >= max_rounds:
        next_idx = current_phase_idx + 1
        reason = f"max_rounds={max_rounds} reached for {name}"
        if next_idx < len(pipeline):
            return True, next_idx, reason
        return True, None, reason

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
