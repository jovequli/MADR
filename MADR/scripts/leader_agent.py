#!/usr/bin/env python3
"""Leader Agent: LLM-driven coordinator for multi-basin calibration.

The leader maintains full round history, analyzes worker results,
and provides per-worker guidance for the next round.
"""

from __future__ import annotations
from .prompt_source import PromptSource
from .context_delivery import (validate_context_delivery_protocol, render_context_system, build_leader_prompt)
from .prompt_visibility import validate_prompt_visibility_protocol, visibility_metadata
from .experiment_contract import normalize_guidance

from typing import Any, Dict, List, Optional, Tuple

from .llm_client import LLMClient
from .llm_failure_policy import is_historical_fallback
from .param_validator import (
    PARAM_KEYS,
    DEFAULT_RANGES,
    CalibrationPhase,
    get_locked_keys,
)

from .execution_protocol import global_score, is_finite


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------

class LeaderMemory:
    """Leader history with bounded window.

    rounds: only keeps last max_rounds_window detailed entries (default 5).
    j_history/nse_history: compact full-run lists (float only, minimal cost).
    """

    def __init__(self, max_rounds_window: int = 5):
        self.max_rounds_window = max_rounds_window
        self.rounds: List[Dict[str, Any]] = []  # last N rounds with detail
        self.j_history: List[float] = []          # full run J_total
        self.outlet_nse_history: List[float] = [] # full run outlet NSE
        self.mean_sub_nse_history: List[float] = []
        self.best_global: Dict[str, Any] = {
            "score": float("-inf"),
            "round_id": -1,
            "params_by_subbasin": {},
        }
        self.latest_best_by_subbasin: Dict[str, Dict] = {}

    def add_round(
        self,
        round_id: int,
        worker_results: List[Dict],
        j_total: float,
        outlet_nse: float,
        mean_sub_nse: float,
        consistency_penalty: float,
        leader_decision: Dict,
    ):
        entry = {
            "round_id": round_id,
            "worker_results": worker_results,
            "j_total": j_total,
            "outlet_nse": outlet_nse,
            "mean_sub_nse": mean_sub_nse,
            "consistency_penalty": consistency_penalty,
            "leader_decision": leader_decision,
        }
        self.rounds.append(entry)
        # Trim rounds window (keep last N detailed entries)
        if len(self.rounds) > self.max_rounds_window:
            self.rounds = self.rounds[-self.max_rounds_window:]
        self.j_history.append(j_total)
        self.outlet_nse_history.append(outlet_nse)
        self.mean_sub_nse_history.append(mean_sub_nse)

        if is_finite(j_total) and j_total > self.best_global["score"]:
            self.best_global = {
                "score": j_total,
                "round_id": round_id,
                "params_by_subbasin": {
                    r["subbasin_id"]: r.get("best_params", r.get("params", {}))
                    for r in worker_results
                },
            }

        for r in worker_results:
            if r.get("ok") is not True or not is_finite(r.get("metrics", {}).get("NSE")):
                continue
            sid = r["subbasin_id"]
            self.latest_best_by_subbasin[sid] = {
                "round_id": round_id,
                "params": r.get("best_params", r.get("params", {})),
                "metrics": r.get("metrics", {}),
            }

    def format_full_history_table(self) -> str:
        """All-round J with station details from the retained recent window."""
        if not self.j_history:
            return "(no history yet)"

        # Collect all basin IDs in order
        basin_ids = []
        seen = set()
        for entry in self.rounds:
            for wr in entry.get("worker_results", []):
                sid = wr.get("subbasin_id", "")
                if sid and sid not in seen:
                    basin_ids.append(sid)
                    seen.add(sid)

        # Also check latest_best for basins from rounds that were trimmed
        for sid in self.latest_best_by_subbasin:
            if sid not in seen:
                basin_ids.append(sid)
                seen.add(sid)

        if not basin_ids:
            return "(no history yet)"

        # Build header: R|J_total|basin1_NSE|basin1_PBIAS|basin2_NSE|...
        header_parts = ["R", "J"]
        for sid in basin_ids:
            short = sid[:8]  # Abbreviate basin names
            header_parts.extend([f"{short}_NSE", f"{short}_PB"])
        header = "|".join(header_parts)

        # Build rows from full j_history + whatever round data we have
        lines = ["J is retained for all rounds; station NSE/PBIAS details are retained only for the recent window. "
                 "? means detail not supplied here, not an observed hydrological data gap.", header]
        # We only have detailed data for recent rounds (self.rounds has window),
        # but j_history has all. For trimmed rounds, show J only.
        round_data_by_id = {e["round_id"]: e for e in self.rounds}
        for i, j_val in enumerate(self.j_history):
            rid = i + 1  # round_id starts at 1
            parts = [str(rid), f"{j_val:.4f}"]
            entry = round_data_by_id.get(rid)
            if entry:
                result_by_sid = {wr["subbasin_id"]: wr for wr in entry.get("worker_results", [])}
                for sid in basin_ids:
                    wr = result_by_sid.get(sid)
                    if wr:
                        m = wr.get("metrics", {})
                        nse = m.get("NSE")
                        pb = m.get("PBIAS")
                        parts.append(f"{nse:.3f}" if isinstance(nse, (int, float)) else "?")
                        parts.append(f"{pb:.1f}" if isinstance(pb, (int, float)) else "?")
                    else:
                        parts.extend(["?", "?"])
            else:
                # Trimmed round — no detailed data
                parts.extend(["?", "?"] * len(basin_ids))
            lines.append("|".join(parts))

        from .phase_mc_feedback import compact_mc_history
        mc_history = compact_mc_history([wr.get("experiment_feedback")
            for entry in self.rounds for wr in entry.get("worker_results", [])])
        if mc_history:
            lines.append("Recent MC attempts (including unselected candidates): " + mc_history)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Leader Agent
# ---------------------------------------------------------------------------

class LeaderAgent:
    """LLM-driven leader agent."""

    def __init__(
        self,
        agent_id: str,
        llm_client: LLMClient,
        workers: List[Dict[str, Any]],
        objective_weights: Dict[str, float],
        param_ranges: Optional[Dict[str, list]] = None,
        static_input_protocol=None,
        prompt_visibility_protocol=None,
        context_delivery_protocol=None,
    ):
        self.agent_id = agent_id
        self.llm = llm_client
        self.workers = workers
        self.worker_ids = {w["subbasin_id"]: w for w in workers}
        self.objective_weights = objective_weights
        self.memory = LeaderMemory()

        # Convert list ranges to tuple ranges
        self.ranges: Dict[str, Tuple[float, float]] = {}
        if param_ranges:
            for k, v in param_ranges.items():
                self.ranges[k] = (float(v[0]), float(v[1]))
        else:
            self.ranges = dict(DEFAULT_RANGES)

        self.prompt_visibility_protocol = validate_prompt_visibility_protocol(prompt_visibility_protocol, method="MADR")
        self.context_delivery_protocol = validate_context_delivery_protocol(context_delivery_protocol)
        self.configure_static_input(static_input_protocol)

    def configure_static_input(self, protocol=None):
        """Select validated delivery storage without changing history or behavior."""
        self.prompt_source = PromptSource(protocol)
        self.static_input_protocol = dict(self.prompt_source.protocol)
        self.system_prompt = render_context_system(self.prompt_source, "leader", self.prompt_visibility_protocol, self.context_delivery_protocol)
        self.review_template = self.prompt_source.get_text("leader_user")
        self.static_prompt_metadata = self.prompt_source.provenance_metadata(
            system=self.system_prompt, user_template=self.review_template)
        self.static_prompt_metadata.update(visibility_metadata(self.prompt_visibility_protocol))

    def compute_global_score(self, worker_results):
        return global_score(worker_results, self.workers, self.objective_weights)

    def build_review_prompt(
        self,
        round_id: int,
        worker_results: List[Dict],
        current_center: Dict,
        current_radius: Dict,
        phase: Optional[CalibrationPhase] = None,
        rounds_in_current_phase: int = 0,
        diagnostics_block: Optional[str] = None,
    ) -> Tuple[str, str, float, float, float, float]:
        """Build the review prompt without calling the LLM.

        Project measured state and history using the configured delivery profile.

        Returns:
            (review_prompt, system_prompt, j_total, outlet_nse, mean_sub_nse, consistency_penalty)
        """
        j_total, outlet_nse, mean_sub_nse, consistency_penalty = \
            self.compute_global_score(worker_results)

        review_prompt = build_leader_prompt(self, round_id, worker_results, current_center,
            phase, rounds_in_current_phase, j_total, outlet_nse, mean_sub_nse, diagnostics_block)
        return review_prompt, self.system_prompt, j_total, outlet_nse, mean_sub_nse, consistency_penalty

    def get_worker_guidance(
        self, decision: Dict, subbasin_id: str,
    ) -> Optional[Dict]:
        """Extract per-worker guidance from leader decision.

        Handles LLMs that use alternative field names (worker_name, basin_id, etc.)
        and normalizes the result to have consistent keys.
        """
        guidance_list = decision.get("worker_guidance", [])

        # Pass 1: exact match on subbasin_id
        matched = None
        for g in guidance_list:
            if g.get("subbasin_id") == subbasin_id:
                matched = g
                break

        # Pass 2: try alternative field names
        if matched is None:
            for g in guidance_list:
                for key in ("worker_name", "basin_id", "basin", "name",
                            "sub_basin_id", "basin_name", "worker_id"):
                    if g.get(key) == subbasin_id:
                        matched = g
                        break
                if matched:
                    break

        # Pass 3: case-insensitive substring match on any string value
        if matched is None:
            sid_lower = subbasin_id.lower()
            for g in guidance_list:
                for v in g.values():
                    if isinstance(v, str) and sid_lower in v.lower():
                        matched = g
                        break
                if matched:
                    break

        if matched is None:
            return None

        # Normalize: map alternative field names to canonical ones
        normalized = dict(matched)
        if "subbasin_id" not in normalized:
            normalized["subbasin_id"] = subbasin_id
        if "priority_focus" not in normalized:
            # Try alternative names for priority/focus
            for key in ("focus", "priority", "main_focus", "objective"):
                if key in normalized:
                    normalized["priority_focus"] = normalized[key]
                    break
        if "param_adjustments" not in normalized:
            # Try known alternative names first
            for key in ("adjustments", "parameter_adjustments", "parameter_updates",
                        "parameter_adjustment", "params"):
                if key in normalized and isinstance(normalized[key], list):
                    normalized["param_adjustments"] = normalized[key]
                    break
            # Fallback: find any list field containing dicts with "direction" key
            if "param_adjustments" not in normalized:
                for key, val in normalized.items():
                    if isinstance(val, list) and val and isinstance(val[0], dict) and "direction" in val[0]:
                        normalized["param_adjustments"] = val
                        break
        if "search_radius_hint" not in normalized:
            for key in ("radius_hint", "radius", "search_radius", "search_radius_advice"):
                if key in normalized:
                    normalized["search_radius_hint"] = normalized[key]
                    break
        # If there are free-text instructions, store them for the worker prompt
        if "instructions" in normalized and "priority_focus" not in normalized:
            normalized["priority_focus"] = normalized["instructions"]

        return normalize_guidance(normalized)

    def should_stop(self, decision: Dict) -> bool:
        """Check if leader decided to stop."""
        return decision.get("stop_or_continue") == "stop"

    def _fallback_decision(
        self,
        round_id: int,
        worker_results: List[Dict],
        j_total: float,
        outlet_nse: float,
        mean_sub_nse: float,
        phase: Optional[CalibrationPhase] = None,
    ) -> Dict:
        """Declared code recovery; no LLM success or inferred trial is fabricated."""
        historical = is_historical_fallback(self)
        # Determine trend
        if len(self.memory.j_history) >= 2:
            delta = j_total - self.memory.j_history[-1]
            if delta > 0.003:
                trend = "improving"
            elif delta < -0.003:
                trend = "declining"
            else:
                trend = "plateauing"
        else:
            trend = "improving"

        # No early stopping — all phases run their full allocated rounds
        phase_rec = "stay"

        # Preserve accepted centers and radii without parameter advice.
        locked = set(get_locked_keys(phase)) if phase else set()
        guidance = []
        for wr in worker_results:
            sid = wr["subbasin_id"]
            adjustments = []
            if historical:
                # Rule-based fallback, with missing/nonfinite metrics
                # withheld and the current phase locks retained. These are code
                # trials, not evidence that a physical cause has been identified.
                metrics = wr.get('metrics', {})
                peak, low = metrics.get('peak_ratio'), metrics.get('lowflow_ratio')
                if 'binfilt' not in locked and is_finite(peak):
                    if peak > 1.05:
                        adjustments.append({'param': 'binfilt', 'direction': 'decrease', 'magnitude': 'small',
                                            'reasoning': 'Historical code heuristic: high-flow ratio above threshold.'})
                    elif peak < .95:
                        adjustments.append({'param': 'binfilt', 'direction': 'increase', 'magnitude': 'small',
                                            'reasoning': 'Historical code heuristic: high-flow ratio below threshold.'})
                if 'Ds' not in locked and is_finite(low):
                    if low < .9:
                        adjustments.append({'param': 'Ds', 'direction': 'increase', 'magnitude': 'small',
                                            'reasoning': 'Historical code heuristic: low-flow ratio below threshold.'})
                    elif low > 1.1:
                        adjustments.append({'param': 'Ds', 'direction': 'decrease', 'magnitude': 'small',
                                            'reasoning': 'Historical code heuristic: low-flow ratio above threshold.'})
            # Fill remaining params with "hold"
            adj_params = {a["param"] for a in adjustments}
            for pk in PARAM_KEYS:
                if pk not in adj_params:
                    if pk in locked:
                        adjustments.append({"param": pk, "direction": "hold", "magnitude": "none", "reasoning": "Locked in current phase"})
                    else:
                        adjustments.append({"param": pk, "direction": "hold", "magnitude": "none", "reasoning": "LLM unavailable; no inferred adjustment"})

            guidance.append({
                "subbasin_id": sid,
                "priority_focus": ("Historical code fallback; test the available diagnostic signal" if historical else "LLM unavailable; hold accepted parameters and retain radius"),
                "param_adjustments": adjustments,
                "search_radius_hint": "keep",
            })

        return {
            "round_id": round_id,
            "overall_assessment": f"{'Historical rule' if historical else 'Neutral'} fallback: LLM unavailable. J_total={j_total:.5f}, trend={trend}.",
            "global_score_trend": trend,
            "stop_or_continue": "continue",
            "stop_reason": "",
            "phase_recommendation": phase_rec,
            "worker_guidance": guidance,
            "next_round_strategy": ("Historical rule fallback: code-generated phase-allowed guidance; radius remains inactive, and the next scheduled call retries the LLM." if historical else "Neutral fallback: hold accepted parameters and keep search radius; no LLM diagnostic inference."),
            "_decision_source": "historical_rule_fallback" if historical else "neutral_fallback",
        }
