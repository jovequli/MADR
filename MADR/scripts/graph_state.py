#!/usr/bin/env python3
"""Calibration state fields for the MADR graph."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


# ---------------------------------------------------------------------------
# Phase specification
# ---------------------------------------------------------------------------

class PhaseSpec(TypedDict, total=False):
    """One entry in the configurable phase pipeline."""
    name: str             # e.g. "VIC_ONLY", "ROUTING_ONLY", "JOINT"
    max_rounds: int       # max rounds for this phase
    mc_rounds: int        # how many rounds use multi-candidate mode
    patience_rounds: Optional[int]
    patience_min_delta: float
    mc_candidates: int    # number of candidates per multi-candidate round


# ---------------------------------------------------------------------------
# Main graph state
# ---------------------------------------------------------------------------

class CalibrationState(TypedDict, total=False):
    """Full state passed through the LangGraph calibration graph.

    All fields are top-level (no nested mutable objects as state owners)
    so that LangGraph checkpointing works correctly.
    """

    # ── Configuration (set once in init_calibration) ──
    team_config: Dict[str, Any]
    worker_evidence_mode: str              # existing
    team_config_path: str
    output_root: str
    mode: str                              # "simulate" or "real"
    dry_run: bool
    param_ranges: Dict[str, Any]           # {param: (lo, hi)}
    objective_weights: Dict[str, float]
    stop_rules: Dict[str, Any]
    worker_cfgs: List[Dict[str, Any]]
    upstream_map: Dict[str, List[str]]     # {basin: [upstream_basins]}
    full_upstream_map: Dict[str, List[str]]
    fixed_upstream_routing: Dict[str, Dict]
    max_rounds: int
    max_parallel_workers: int

    # ── Phase pipeline ──
    phase_pipeline: List[PhaseSpec]        # ordered phase specs
    topo_levels: List[List[str]]           # topological batch levels
    current_phase_idx: int
    current_phase_name: str                # e.g. "VIC_ONLY"
    rounds_in_current_phase: int
    phase_history: List[Dict[str, Any]]
    phase_nse_thresholds: Dict[str, float]

    # ── Round tracking ──
    round_id: int
    is_final_round: bool
    round_start_time: str                  # ISO timestamp

    # ── Search state (per-basin) ──
    accepted_center_records: Dict[str, Dict]
    last_center_acceptance: Dict[str, bool]
    confirmation_vector: Dict[str, Dict[str, float]]
    confirmation_receipt: Dict[str, Any]
    centers: Dict[str, Dict[str, float]]
    radii: Dict[str, Dict[str, float]]
    direction_history: Dict[str, Dict]     # anti-flip momentum

    # ── Batch processing ──
    current_level_idx: int
    current_batch_sids: List[str]
    batch_worker_results: List[Dict]
    round_worker_results: List[Dict]       # all results for current round

    # ── Multi-candidate ──
    mc_round_summaries: Dict[int, str]     # round_id -> summary lines
    use_multi_candidate: bool
    mc_n_candidates: int
    mc_vic_rounds: int
    mc_rout_rounds: int

    # ── Leader state (stateless per-round, no conversation accumulation) ──
    leader_decision: Dict[str, Any]
    leader_phase_context: Dict[str, Any]    # observed evidence vs next executable guidance identity
    leader_memory_rounds: List[Dict]       # sliding window (last 5 rounds)
    leader_j_history: List[float]          # all rounds
    leader_outlet_nse_history: List[float]
    leader_mean_sub_nse_history: List[float]
    leader_best_global: Dict[str, Any]     # {score, round_id, params_by_subbasin}
    leader_latest_best_by_subbasin: Dict[str, Dict]

    # ── Leader token management ──
    leader_token_budget: int
    leader_tokens: Dict[str, int]             # {prompt, completion, total} from last review

    # ── Worker memories ──
    worker_memories: Dict[str, Dict]       # {sid: WorkerMemory.to_checkpoint()}

    # ── Diagnostics ──
    diagnostic_reports: List[Dict]
    diagnostics_prompt_block: str
    coordination_result: Dict[str, Any]

    # ── Exploration ──
    exploration_results: Dict[str, Any]
    exploration_summary: str

    # ── Escape states (SADR rule Worker) ──
    escape_states: Dict[str, Dict]

    # ── Communication counters ──
    comm_totals: Dict[str, int]

    # ── Round output tracking ──
    round_dir: str                         # path to current round directory

    # ── Control flow signals ──
    should_continue: bool                  # False → go to finalize
    force_final_round: bool
    best_mean_nse_in_phase: Optional[float]
    rounds_without_improvement_in_phase: int
    phase_transitioned: bool               # True if phase just changed

    # ── Worker execution context (for Send()) ──
    subbasin_id: str                       # set by Send() for worker_execute

    # ── Final outputs ──
    final_summary: Dict[str, Any]
