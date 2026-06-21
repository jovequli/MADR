#!/usr/bin/env python3
"""Leader Agent: LLM-driven coordinator for multi-basin calibration.

The leader maintains full round history, analyzes worker results,
and provides per-worker guidance for the next round.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .llm_client import LLMClient, LLMResponse, create_client, LLMClientConfig
from .param_validator import (
    PARAM_KEYS,
    SOIL_PARAM_KEYS,
    ROUTING_PARAM_KEYS,
    DEFAULT_RANGES,
    CalibrationPhase,
    get_tunable_keys,
    get_locked_keys,
    normalize_params,
    validate_leader_output,
    safe_apply_leader_guidance,
    clamp,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------

class LeaderMemory:
    """Leader history with bounded window.

    rounds: only keeps last max_rounds_window detailed entries (default 5).
    j_history/nse_history: compact full-run lists (float only, minimal cost).
    recent_summaries: current-round worker summaries (overwritten each round).
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
        self.llm_conversation: List[Dict[str, str]] = []
        # Worker summaries (current round only, injected by orchestrator)
        self.recent_summaries: List[Dict[str, Any]] = []
        # Cross-basin coordination result (current round only)
        self.coordination_compact: str = ""

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
        # Trim rounds window (keep only recent N rounds with full detail)
        if len(self.rounds) > self.max_rounds_window:
            self.rounds = self.rounds[-self.max_rounds_window:]
        self.j_history.append(j_total)
        self.outlet_nse_history.append(outlet_nse)
        self.mean_sub_nse_history.append(mean_sub_nse)

        if j_total > self.best_global["score"]:
            self.best_global = {
                "score": j_total,
                "round_id": round_id,
                "params_by_subbasin": {
                    r["subbasin_id"]: r.get("best_params", r.get("params", {}))
                    for r in worker_results
                },
            }

        for r in worker_results:
            sid = r["subbasin_id"]
            self.latest_best_by_subbasin[sid] = {
                "round_id": round_id,
                "params": r.get("best_params", r.get("params", {})),
                "metrics": r.get("metrics", {}),
            }

    def format_history_block(self) -> str:
        """Format full history for leader prompt."""
        if not self.rounds:
            return "No previous rounds."
        lines = []
        for entry in self.rounds:
            rid = entry["round_id"]
            lines.append(
                f"Round {rid}: J_total={entry['j_total']:.5f}, "
                f"outlet_NSE={entry['outlet_nse']:.4f}, "
                f"mean_sub_NSE={entry['mean_sub_nse']:.4f}"
            )
            for wr in entry["worker_results"]:
                m = wr.get("metrics", {})
                lines.append(
                    f"  {wr['subbasin_id']}: NSE={m.get('NSE', 'N/A')}, "
                    f"PBIAS={m.get('PBIAS', 'N/A')}, "
                    f"peak_ratio={m.get('peak_ratio', 'N/A')}, "
                    f"peak_timing={m.get('peak_timing_bias_days', 'N/A')}d, "
                    f"recession_k={m.get('recession_k_ratio', 'N/A')}"
                )
            decision = entry.get("leader_decision", {})
            strategy = decision.get("next_round_strategy", "")
            if strategy:
                lines.append(f"  Strategy: {strategy}")
        return "\n".join(lines)

    def format_param_trajectory_for_joint(self, n_recent: int = 10) -> str:
        """Format per-basin parameter trajectory for JOINT phase fine-tuning.

        Shows the last n_recent rounds with all 8 params + NSE per basin,
        so the Leader can reason about which directions worked.
        """
        if not self.rounds:
            return ""

        recent_rounds = self.rounds[-n_recent:]
        if not recent_rounds:
            return ""

        basin_ids = []
        seen = set()
        for entry in recent_rounds:
            for wr in entry.get("worker_results", []):
                sid = wr.get("subbasin_id", "")
                if sid and sid not in seen:
                    basin_ids.append(sid)
                    seen.add(sid)
        if not basin_ids:
            return ""

        def _f(v, fmt):
            if v is None:
                return "?"
            try:
                fv = float(v)
                if fv != fv:
                    return "?"
                return format(fv, fmt)
            except (ValueError, TypeError):
                return "?"

        chunks = []
        for sid in basin_ids:
            chunks.append(f"### {sid}")
            chunks.append("R|binfilt|Ds|Dsmax|Ws|d2|d3|vel|diff|NSE|PBIAS")
            for entry in recent_rounds:
                rid = entry.get("round_id", "?")
                result_by_sid = {wr["subbasin_id"]: wr for wr in entry.get("worker_results", [])}
                wr = result_by_sid.get(sid)
                if not wr:
                    continue
                p = wr.get("best_params", wr.get("params", {}))
                m = wr.get("metrics", {})
                chunks.append(
                    f"{rid}|{_f(p.get('binfilt'), '.3f')}|{_f(p.get('Ds'), '.4f')}|"
                    f"{_f(p.get('Dsmax'), '.1f')}|{_f(p.get('Ws'), '.2f')}|"
                    f"{_f(p.get('soil_d2'), '.2f')}|{_f(p.get('soil_d3'), '.2f')}|"
                    f"{_f(p.get('velocity'), '.2f')}|{_f(p.get('diffusivity'), '.0f')}|"
                    f"{_f(m.get('NSE'), '.3f')}|{_f(m.get('PBIAS'), '.1f')}"
                )
            chunks.append("")
        return "\n".join(chunks)

    def format_full_history_table(self) -> str:
        """Format ALL rounds as a compact metrics-only table (~15 tokens/row per basin).

        V3: Replaces verbose history_block. Leader sees full trajectory for ALL basins.
        Only includes NSE + PBIAS (the 2 most actionable metrics).
        """
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

        for sid in self.latest_best_by_subbasin:
            if sid not in seen:
                basin_ids.append(sid)
                seen.add(sid)

        if not basin_ids:
            return "(no history yet)"

        # Header: R|J|basin1_NSE|basin1_PB|basin2_NSE|...
        header_parts = ["R", "J"]
        for sid in basin_ids:
            short = sid[:8]
            header_parts.extend([f"{short}_NSE", f"{short}_PB"])
        header = "|".join(header_parts)

        lines = [header]
        round_data_by_id = {e["round_id"]: e for e in self.rounds}
        for i, j_val in enumerate(self.j_history):
            rid = i + 1
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
                parts.extend(["?", "?"] * len(basin_ids))
            lines.append("|".join(parts))

        return "\n".join(lines)

    def format_worker_results_block(self, worker_results: List[Dict]) -> str:
        """Format current round worker results for prompt."""
        lines = []
        for wr in worker_results:
            m = wr.get("metrics", {})
            p = wr.get("best_params", wr.get("params", {}))
            lines.append(f"### {wr['subbasin_id']} ({wr.get('station_code', '')})")
            lines.append(f"- NSE: {m.get('NSE', 'N/A')}")
            lines.append(f"- RMSE: {m.get('RMSE', 'N/A')}")
            lines.append(f"- PBIAS: {m.get('PBIAS', 'N/A')}%")
            lines.append(f"- peak_ratio: {m.get('peak_ratio', 'N/A')}")
            lines.append(f"- lowflow_ratio: {m.get('lowflow_ratio', 'N/A')}")
            lines.append(f"- peak_timing_bias: {m.get('peak_timing_bias_days', 'N/A')} days")
            lines.append(f"- peak_magnitude_bias: {m.get('peak_magnitude_bias_pct', 'N/A')}%")
            lines.append(f"- recession_k_ratio: {m.get('recession_k_ratio', 'N/A')}")
            lines.append(f"- baseflow_index_bias: {m.get('baseflow_index_bias_pct', 'N/A')}%")
            lines.append(f"- Parameters used: {json.dumps(p)}")
            analysis = wr.get("analysis", "")
            if analysis:
                lines.append(f"- Worker analysis: {analysis}")
            lines.append("")
        return "\n".join(lines)

    def format_worker_summaries_block(self) -> str:
        """Format worker summaries (~200 tokens)."""
        if not self.recent_summaries:
            return "No worker summaries available."
        lines = []
        for s in self.recent_summaries:
            sid = s.get("subbasin_id", "?")
            nse = s.get("current_nse")
            nse_str = f"{nse:.4f}" if nse is not None and nse > -999 else "N/A"
            issue = s.get("primary_issue", "none")
            trend = s.get("improvement_trend", "?")
            stag = s.get("stagnation_count", 0)
            direction = s.get("suggested_direction", "")
            lines.append(f"  {sid}: NSE={nse_str}, issue={issue}, trend={trend}, stagnant={stag}r")
            if direction:
                lines.append(f"    diag: {direction[:100]}")
        return "\n".join(lines)

    def format_coordination_block(self) -> str:
        """Format cross-basin coordination results."""
        return self.coordination_compact if self.coordination_compact else "No cross-basin insights."

    def format_locked_params_block(self) -> str:
        """Format locked upstream parameters."""
        if not self.latest_best_by_subbasin:
            return "No parameters locked yet."
        lines = []
        for sid, info in self.latest_best_by_subbasin.items():
            lines.append(
                f"- {sid} (from round {info['round_id']}): "
                f"{json.dumps(info['params'])}"
            )
        return "\n".join(lines)

    def trim_conversation(self):
        """Trim LLM conversation history to match rounds window."""
        max_msgs = self.max_rounds_window * 2  # user + assistant per round
        if len(self.llm_conversation) > max_msgs:
            self.llm_conversation = self.llm_conversation[-max_msgs:]

    def to_checkpoint(self) -> Dict:
        """Serialize memory for checkpoint/resume."""
        return {
            "rounds": self.rounds,
            "j_history": self.j_history,
            "outlet_nse_history": self.outlet_nse_history,
            "mean_sub_nse_history": self.mean_sub_nse_history,
            "best_global": self.best_global,
            "latest_best_by_subbasin": self.latest_best_by_subbasin,
            "llm_conversation": self.llm_conversation,
        }

    @classmethod
    def from_checkpoint(cls, data: Dict) -> "LeaderMemory":
        """Restore from checkpoint."""
        mem = cls()
        mem.rounds = data.get("rounds", [])
        mem.j_history = data.get("j_history", [])
        mem.outlet_nse_history = data.get("outlet_nse_history", [])
        mem.mean_sub_nse_history = data.get("mean_sub_nse_history", [])
        mem.best_global = data.get("best_global", mem.best_global)
        mem.latest_best_by_subbasin = data.get("latest_best_by_subbasin", {})
        mem.llm_conversation = data.get("llm_conversation", [])
        return mem


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

        # Load prompts
        self.system_prompt = _load_text(PROMPTS_DIR / "leader_system.md")
        self.review_template = _load_text(PROMPTS_DIR / "leader_review.md")

        # Append parameter knowledge if available
        param_knowledge_path = PROMPTS_DIR / "param_knowledge_leader.md"
        if param_knowledge_path.exists():
            self.system_prompt += "\n\n" + _load_text(param_knowledge_path)

        # Load schema for validation
        self.output_schema = _load_json(SCHEMAS_DIR / "leader_output.json")

    def compute_global_score(
        self, worker_results: List[Dict],
    ) -> Tuple[float, float, float, float]:
        """Compute global objective from worker results.

        All stations are weighted equally in the mean NSE.
        Returns (j_total, outlet_nse, mean_all_nse, consistency_penalty).
        """
        outlet_ids = {w["subbasin_id"] for w in self.workers if w.get("is_outlet")}

        all_nses = []
        outlet_nses = []
        consistency_terms = []

        for wr in worker_results:
            m = wr.get("metrics", {})
            nse = m.get("NSE", float("-inf"))
            if isinstance(nse, (int, float)) and nse == nse:  # not NaN
                sid = wr["subbasin_id"]
                all_nses.append(nse)
                if sid in outlet_ids:
                    outlet_nses.append(nse)
                pr = m.get("peak_ratio", 1.0)
                lr = m.get("lowflow_ratio", 1.0)
                if isinstance(pr, (int, float)) and pr == pr:
                    consistency_terms.append(abs(pr - 1.0))
                if isinstance(lr, (int, float)) and lr == lr:
                    consistency_terms.append(abs(lr - 1.0))

        mean_all = sum(all_nses) / len(all_nses) if all_nses else 0.0
        mean_outlet = sum(outlet_nses) / len(outlet_nses) if outlet_nses else 0.0
        consistency = sum(consistency_terms) / len(consistency_terms) if consistency_terms else 0.0

        w = self.objective_weights
        j_total = (
            w.get("mean_nse", 0.90) * mean_all
            - w.get("consistency", 0.10) * consistency
        )
        return j_total, mean_outlet, mean_all, consistency

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

        V3: Uses full history table instead of verbose per-round blocks.
        Removes centers/radii/weights JSON (saves ~750 tokens).

        Returns:
            (review_prompt, system_prompt, j_total, outlet_nse, mean_sub_nse, consistency_penalty)
        """
        j_total, outlet_nse, mean_sub_nse, consistency_penalty = \
            self.compute_global_score(worker_results)

        if diagnostics_block:
            results_block = diagnostics_block
        else:
            results_block = self.memory.format_worker_results_block(worker_results)

        history_table = self.memory.format_full_history_table()

        review_prompt = self.review_template.format(
            round_id=round_id,
            worker_results_block=results_block,
            history_table=history_table,
            j_total=j_total,
            outlet_nse=outlet_nse,
            mean_sub_nse=mean_sub_nse,
        )

        if phase:
            tunable = get_tunable_keys(phase)
            locked = get_locked_keys(phase)
            review_prompt += (
                f"\nPhase: {phase.value} (round {rounds_in_current_phase}). "
                f"Tunable: {', '.join(tunable)}. "
                f"Locked: {', '.join(locked) if locked else 'none'}.\n"
            )
            if phase == CalibrationPhase.VIC_ONLY:
                review_prompt += (
                    "VIC_ONLY: Optimize 6 soil params for NSE. "
                    "Routing timing won't be perfect yet — expected. "
                    "Always continue, never stop or advance.\n"
                )
            elif phase == CalibrationPhase.ROUTING_ONLY:
                review_prompt += (
                    "ROUTING_ONLY: Tune velocity/diffusivity for timing. "
                    "Always continue, never stop.\n"
                )
            elif phase == CalibrationPhase.JOINT:
                review_prompt += (
                    "JOINT: Fine-tune all 8 params. Small adjustments. "
                    "Always continue.\n"
                )
                # During JOINT, inject per-basin parameter trajectory
                # so the Leader can see what has been tried.
                param_trajectory = self.memory.format_param_trajectory_for_joint(n_recent=10)
                if param_trajectory:
                    review_prompt += "\n## Recent Parameter Trajectory (last 10 rounds)\n"
                    review_prompt += param_trajectory + "\n"

        return review_prompt, self.system_prompt, j_total, outlet_nse, mean_sub_nse, consistency_penalty

    def review_round(
        self,
        round_id: int,
        worker_results: List[Dict],
        current_center: Dict,
        current_radius: Dict,
        phase: Optional[CalibrationPhase] = None,
        rounds_in_current_phase: int = 0,
        diagnostics_block: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], LLMResponse]:
        """Have the leader LLM review the round and produce guidance.

        V3: Delegates to build_review_prompt() for compact prompt construction.

        Returns (decision_dict, llm_response).
        """
        review_prompt, _, j_total, outlet_nse, mean_sub_nse, consistency_penalty = \
            self.build_review_prompt(
                round_id=round_id,
                worker_results=worker_results,
                current_center=current_center,
                current_radius=current_radius,
                phase=phase,
                rounds_in_current_phase=rounds_in_current_phase,
                diagnostics_block=diagnostics_block,
            )

        # Build conversation messages
        # Include prior conversation history for full context
        messages = list(self.memory.llm_conversation)
        messages.append({"role": "user", "content": review_prompt})

        # Call LLM (with fallback on any failure)
        decision = None
        try:
            llm_resp = self.llm.chat(
                messages=messages,
                system=self.system_prompt,
            )
            # Validate and parse
            decision = llm_resp.parsed
            if decision is not None:
                validation = validate_leader_output(decision)
                if not validation.is_valid:
                    logger.warning("Leader output validation failed: %s. Using fallback.", validation.errors)
                    decision = None
                else:
                    if validation.warnings:
                        logger.info("Leader output validation warnings: %s", validation.warnings)
                    # Inject round_id if LLM omitted it
                    if "round_id" not in decision:
                        decision["round_id"] = round_id
                    # Ensure stop_or_continue has a valid value
                    if decision.get("stop_or_continue") not in ("continue", "stop"):
                        decision["stop_or_continue"] = "continue"
            if decision is None:
                logger.warning("Leader LLM returned non-JSON response. Using fallback.")
        except Exception as e:
            logger.error("Leader LLM call failed: %s. Using fallback.", e)
            llm_resp = LLMResponse(content="", provider="fallback")

        if decision is None:
            decision = self._fallback_decision(round_id, worker_results, j_total, outlet_nse, mean_sub_nse, phase=phase)

        # Update conversation history (with trimming)
        self.memory.llm_conversation.append({"role": "user", "content": review_prompt})
        self.memory.llm_conversation.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        self.memory.trim_conversation()

        # Update memory
        self.memory.add_round(
            round_id=round_id,
            worker_results=worker_results,
            j_total=j_total,
            outlet_nse=outlet_nse,
            mean_sub_nse=mean_sub_nse,
            consistency_penalty=consistency_penalty,
            leader_decision=decision,
        )

        return decision, llm_resp

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

        return normalized

    def _fallback_decision(
        self,
        round_id: int,
        worker_results: List[Dict],
        j_total: float,
        outlet_nse: float,
        mean_sub_nse: float,
        phase: Optional[CalibrationPhase] = None,
    ) -> Dict:
        """Rule-based fallback when LLM fails."""
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

        # Phase termination is owned by check_phase_transition_node (per-phase
        # patience / max_rounds). The leader's recommendation is informational.
        phase_rec = "stay"

        # Build per-worker guidance (simple rule-based)
        locked = set(get_locked_keys(phase)) if phase else set()
        guidance = []
        improved = j_total > (self.memory.j_history[-1] if self.memory.j_history else float("-inf"))
        for wr in worker_results:
            sid = wr["subbasin_id"]
            m = wr.get("metrics", {})
            adjustments = []
            # Simple heuristic based on metrics (only for tunable params)
            pr = m.get("peak_ratio", 1.0)
            lr = m.get("lowflow_ratio", 1.0)
            pbias = m.get("PBIAS", 0.0)

            if "binfilt" not in locked:
                if isinstance(pr, (int, float)) and pr > 1.05:
                    adjustments.append({"param": "binfilt", "direction": "decrease", "magnitude": "small", "reasoning": "Peak too high"})
                elif isinstance(pr, (int, float)) and pr < 0.95:
                    adjustments.append({"param": "binfilt", "direction": "increase", "magnitude": "small", "reasoning": "Peak too low"})

            if "Ds" not in locked:
                if isinstance(lr, (int, float)) and lr < 0.9:
                    adjustments.append({"param": "Ds", "direction": "increase", "magnitude": "small", "reasoning": "Low flow too low"})
                elif isinstance(lr, (int, float)) and lr > 1.1:
                    adjustments.append({"param": "Ds", "direction": "decrease", "magnitude": "small", "reasoning": "Low flow too high"})

            # Fill remaining params with "hold"
            adj_params = {a["param"] for a in adjustments}
            for pk in PARAM_KEYS:
                if pk not in adj_params:
                    if pk in locked:
                        adjustments.append({"param": pk, "direction": "hold", "magnitude": "none", "reasoning": "Locked in current phase"})
                    else:
                        adjustments.append({"param": pk, "direction": "hold", "magnitude": "none", "reasoning": "No clear signal"})

            guidance.append({
                "subbasin_id": sid,
                "priority_focus": "improve overall fit",
                "param_adjustments": adjustments,
                "search_radius_hint": "shrink" if improved else "expand",
            })

        return {
            "round_id": round_id,
            "overall_assessment": f"Fallback rule-based decision. J_total={j_total:.5f}, trend={trend}.",
            "global_score_trend": trend,
            "stop_or_continue": "continue",
            "stop_reason": "",
            "phase_recommendation": phase_rec,
            "worker_guidance": guidance,
            "next_round_strategy": "Rule-based fallback: " + ("shrink radius" if improved else "expand radius"),
        }
