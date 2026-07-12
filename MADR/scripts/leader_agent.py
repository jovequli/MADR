#!/usr/bin/env python3
"""Leader Agent: LLM-driven coordinator for multi-basin calibration.

The leader maintains full round history, analyzes worker results,
and provides per-worker guidance for the next round.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .llm_client import LLMClient, LLMResponse, create_client, LLMClientConfig
from .param_validator import (
    PARAM_KEYS,
    SOIL_PARAM_KEYS,
    ROUTING_PARAM_KEYS,
    CAMA_PARAM_KEYS,
    SWAT_PARAM_KEYS,
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

from .prompt_loader import resolve_prompts_dir

SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


# Trajectory table columns per model combination: (param_key, header, fmt).
# vic_routing must stay byte-identical to the legacy 8-param layout.
# swat columns are built dynamically from the varied params (see
# LeaderMemory._varied_swat_columns).
TRAJECTORY_COLUMNS: Dict[str, List[Tuple[str, str, str]]] = {
    "vic_routing": [
        ("binfilt", "binfilt", ".3f"), ("Ds", "Ds", ".4f"),
        ("Dsmax", "Dsmax", ".1f"), ("Ws", "Ws", ".2f"),
        ("soil_d2", "d2", ".2f"), ("soil_d3", "d3", ".2f"),
        ("velocity", "vel", ".2f"), ("diffusivity", "diff", ".0f"),
    ],
    "vic_cama_flood": [
        ("binfilt", "binfilt", ".3f"), ("Ds", "Ds", ".4f"),
        ("Dsmax", "Dsmax", ".1f"), ("Ws", "Ws", ".2f"),
        ("soil_d2", "d2", ".2f"), ("soil_d3", "d3", ".2f"),
        ("Hc", "Hc", ".3f"), ("Hmin", "Hmin", ".3f"),
        ("Wc", "Wc", ".3f"), ("Wmin", "Wmin", ".3f"),
    ],
}


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
        # Current-round worker summaries (injected by orchestrator)
        self.recent_summaries: List[Dict[str, Any]] = []
        # Current-round cross-basin coordination result
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
        # Trim rounds window (keep last N detailed entries)
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
        """Format full history for leader prompt — legacy verbose format."""
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

    @staticmethod
    def _varied_swat_columns(
        recent_rounds: List[Dict[str, Any]], max_cols: int = 10,
    ) -> List[Tuple[str, str, str]]:
        """Pick SWAT params whose values changed across the recorded rounds.

        With 18 SWAT+ params a full table is too wide; show only the params
        that actually moved (per basin, across rounds), capped at max_cols.
        Falls back to the first max_cols known params when nothing varied yet.
        """
        values: Dict[Tuple[str, str], set] = {}
        keys_seen: List[str] = []
        for entry in recent_rounds:
            for wr in entry.get("worker_results", []):
                sid = wr.get("subbasin_id", "")
                p = wr.get("best_params", wr.get("params", {}))
                for k, v in p.items():
                    try:
                        fv = round(float(v), 6)
                    except (ValueError, TypeError):
                        continue
                    values.setdefault((sid, k), set()).add(fv)
                    if k not in keys_seen:
                        keys_seen.append(k)
        # Canonical order first, then any extra recorded keys
        ordered = [k for k in SWAT_PARAM_KEYS if k in keys_seen]
        ordered += [k for k in keys_seen if k not in SWAT_PARAM_KEYS]
        varied = [
            k for k in ordered
            if any(len(vals) > 1 for (sid, key), vals in values.items() if key == k)
        ]
        keys = (varied or ordered)[:max_cols]
        return [(k, k, ".4g") for k in keys]

    def format_param_trajectory_for_joint(
        self, n_recent: int = 10, model_combination: Optional[str] = None,
    ) -> str:
        """Format per-basin parameter trajectory for JOINT phase fine-tuning.

        Shows the last n_recent rounds with the combination's tunable params
        + NSE per basin, so the Leader can reason about which directions
        worked. Columns follow model_combination (default: vic_routing).
        """
        if not self.rounds:
            return ""

        # Use only the in-window recent rounds (memory has bounded window)
        recent_rounds = self.rounds[-n_recent:]
        if not recent_rounds:
            return ""

        # Collect basin IDs from the window
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

        # Select param columns for the active model combination
        combo = model_combination or "vic_routing"
        if combo == "swat":
            columns = self._varied_swat_columns(recent_rounds)
        else:
            columns = TRAJECTORY_COLUMNS.get(combo, TRAJECTORY_COLUMNS["vic_routing"])
        header = "R|" + "|".join(label for _, label, _ in columns) + "|NSE|PBIAS"

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
            chunks.append(header)
            for entry in recent_rounds:
                rid = entry.get("round_id", "?")
                result_by_sid = {wr["subbasin_id"]: wr for wr in entry.get("worker_results", [])}
                wr = result_by_sid.get(sid)
                if not wr:
                    continue
                p = wr.get("best_params", wr.get("params", {}))
                m = wr.get("metrics", {})
                cells = [f"{rid}"]
                cells += [_f(p.get(key), fmt) for key, _, fmt in columns]
                cells.append(_f(m.get('NSE'), '.3f'))
                cells.append(_f(m.get('PBIAS'), '.1f'))
                chunks.append("|".join(cells))
            chunks.append("")
        return "\n".join(chunks)

    def format_full_history_table(self) -> str:
        """Format ALL rounds as a compact metrics-only table (~15 tokens/row per basin).

        V3: Replaces verbose history_block. Leader sees full trajectory for ALL basins.
        Only includes NSE + PBIAS + peak_ratio (the 3 most actionable metrics).
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
        lines = [header]
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
        """Format Worker summaries (lightweight, ~200 tokens)."""
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
                lines.append(f"    hint: {direction[:100]}")
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
        stop_rules: Dict[str, Any],
        param_ranges: Optional[Dict[str, list]] = None,
        model_combination: Optional[str] = None,
        topology_note: str = "",
        n_basins: Optional[int] = None,
        outlet_id: str = "",
    ):
        self.agent_id = agent_id
        self.llm = llm_client
        self.workers = workers
        self.worker_ids = {w["subbasin_id"]: w for w in workers}
        self.objective_weights = objective_weights
        self.stop_rules = stop_rules
        self.memory = LeaderMemory()

        # Convert list ranges to tuple ranges
        self.ranges: Dict[str, Tuple[float, float]] = {}
        if param_ranges:
            for k, v in param_ranges.items():
                self.ranges[k] = (float(v[0]), float(v[1]))
        else:
            self.ranges = dict(DEFAULT_RANGES)

        # Resolve the prompts directory for the active model combination.
        prompts_dir = resolve_prompts_dir(model_combination)
        self.model_combination = prompts_dir.name

        # Load prompts
        self.system_prompt = _load_text(prompts_dir / "leader_system.md")
        self.review_template = _load_text(prompts_dir / "leader_review.md")

        # Append parameter knowledge if available
        param_knowledge_path = prompts_dir / "param_knowledge_leader.md"
        if param_knowledge_path.exists():
            self.system_prompt += "\n\n" + _load_text(param_knowledge_path)

        # Substitute topology placeholders in the system prompt. str.replace
        # is a no-op for files without the tokens; when no topology info was
        # provided, tokens collapse to a generic pointer sentence.
        generic = ("The basin set and routing topology for this run "
                   "are given in the round context.")
        for token, value in (
            ("{{BASIN_TOPOLOGY}}", topology_note or generic),
            ("{{N_BASINS}}", str(n_basins) if n_basins is not None else generic),
            ("{{OUTLET_ID}}", outlet_id or generic),
        ):
            self.system_prompt = self.system_prompt.replace(token, value)

        # Load schema for validation
        schema_path = SCHEMAS_DIR / "leader_output.json"
        self.output_schema = _load_json(schema_path) if schema_path.exists() else None

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
                # Skip missing or non-finite ratios instead of defaulting to a
                # perfect 1.0 (which would dilute the consistency penalty).
                # The SWAT runner does not compute these ratios yet; the real
                # fix (computing them) lives runner-side.
                pr = m.get("peak_ratio")
                lr = m.get("lowflow_ratio")
                if isinstance(pr, (int, float)) and math.isfinite(pr):
                    consistency_terms.append(abs(pr - 1.0))
                if isinstance(lr, (int, float)) and math.isfinite(lr):
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
        expected_subbasins: Optional[List[str]] = None,
    ) -> Tuple[str, str, float, float, float, float]:
        """Build the review prompt without calling the LLM.

        V3: Uses full history table instead of verbose per-round blocks.
        Removes centers/radii/weights/stop_rules JSON (saves ~750 tokens).

        When ``expected_subbasins`` is provided and any listed basin is missing
        from worker_results or has a non-finite NSE, the round is invalidated:
        mean_sub_nse and j_total become NaN. Otherwise a crashed low-NSE site
        drops out of the mean's denominator and the mean can *increase*. NaN
        propagates as not-an-improvement (nan > best is False), so the round
        can never become best and patience counters still increment.
        Default None keeps legacy behaviour.

        Returns:
            (review_prompt, system_prompt, j_total, outlet_nse, mean_sub_nse, consistency_penalty)
        """
        j_total, outlet_nse, mean_sub_nse, consistency_penalty = \
            self.compute_global_score(worker_results)

        failed_basins: List[str] = []
        if expected_subbasins is not None:
            result_by_sid = {wr.get("subbasin_id"): wr for wr in worker_results}
            for sid in expected_subbasins:
                wr = result_by_sid.get(sid)
                nse = wr.get("metrics", {}).get("NSE") if wr else None
                if not (isinstance(nse, (int, float)) and math.isfinite(nse)):
                    failed_basins.append(sid)
            if failed_basins:
                mean_sub_nse = float("nan")
                j_total = float("nan")

        # Diagnostics for current round (from LeaderCoordinator)
        if diagnostics_block:
            results_block = diagnostics_block
        else:
            results_block = self.memory.format_worker_results_block(worker_results)

        # Full history table (all rounds, compact)
        history_table = self.memory.format_full_history_table()

        # NOTE: NaN j_total/mean_sub_nse format safely as "nan" via the
        # template's {j_total:.5f}-style fields — no crash.
        review_prompt = self.review_template.format(
            round_id=round_id,
            worker_results_block=results_block,
            history_table=history_table,
            j_total=j_total,
            outlet_nse=outlet_nse,
            mean_sub_nse=mean_sub_nse,
        )

        if failed_basins:
            review_prompt += (
                f"\nWARNING: Round {round_id} is INVALID — these basins failed "
                f"(missing result or non-finite NSE): {', '.join(failed_basins)}. "
                "J_total and mean_sub_NSE are NaN for this round. Treat it as a "
                "failed round, NOT a mediocre one; do not draw conclusions about "
                "parameter directions from this round's global score.\n"
            )

        # Phase context (compact)
        if phase:
            # Combination-aware tunable/locked lists; fall back to the legacy
            # signature if param_validator is not combination-aware yet.
            try:
                tunable = get_tunable_keys(phase, model_combination=self.model_combination)
                locked = get_locked_keys(phase, model_combination=self.model_combination)
            except TypeError:
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
                    f"JOINT: Fine-tune all {len(tunable)} params. Small adjustments. "
                    "Always continue.\n"
                )
                # During JOINT, inject per-basin parameter trajectory
                # so the Leader can see what has been tried.
                param_trajectory = self.memory.format_param_trajectory_for_joint(
                    n_recent=10, model_combination=self.model_combination)
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
        expected_subbasins: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], LLMResponse]:
        """Have the leader LLM review the round and produce guidance.

        V3: Uses compact full history table, no centers/radii JSON.

        Returns (decision_dict, llm_response).
        """
        # Use build_review_prompt for the actual prompt construction
        review_prompt, _, j_total, outlet_nse, mean_sub_nse, consistency_penalty = \
            self.build_review_prompt(
                round_id=round_id,
                worker_results=worker_results,
                current_center=current_center,
                current_radius=current_radius,
                phase=phase,
                rounds_in_current_phase=rounds_in_current_phase,
                diagnostics_block=diagnostics_block,
                expected_subbasins=expected_subbasins,
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
                json_schema=self.output_schema,
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

        # Check stop conditions — only allowed in JOINT phase
        target = float(self.stop_rules.get("outlet_nse_target", 0.85))
        patience = int(self.stop_rules.get("patience_rounds", 3))
        min_imp = float(self.stop_rules.get("min_improvement", 0.003))

        # No early stopping — all phases run their full allocated rounds
        phase_rec = "stay"

        # Build per-worker guidance (simple rule-based, per model combination)
        try:
            locked = set(get_locked_keys(phase, model_combination=self.model_combination)) if phase else set()
        except TypeError:
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

            if self.model_combination == "swat":
                # SWAT+ menu (prompts/swat/param_knowledge_leader.md).
                # Canonical PBIAS = 100*sum(sim-obs)/sum(obs): positive = too wet.
                param_menu = list(SWAT_PARAM_KEYS)
                if "cn2" not in locked and isinstance(pbias, (int, float)) and math.isfinite(pbias):
                    if pbias > 10.0:
                        adjustments.append({"param": "cn2", "direction": "decrease", "magnitude": "small", "reasoning": "Too wet (PBIAS > +10): reduce runoff (pctchg)"})
                    elif pbias < -10.0:
                        adjustments.append({"param": "cn2", "direction": "increase", "magnitude": "small", "reasoning": "Too dry (PBIAS < -10): increase runoff (pctchg)"})
                if "surlag" not in locked:
                    if isinstance(pr, (int, float)) and pr > 1.05:
                        adjustments.append({"param": "surlag", "direction": "increase", "magnitude": "small", "reasoning": "Peak too high"})
                    elif isinstance(pr, (int, float)) and pr < 0.95:
                        adjustments.append({"param": "surlag", "direction": "decrease", "magnitude": "small", "reasoning": "Peak too low"})
                # Low-flow menu follows the unified rule (worker_diagnostics /
                # prompts/swat/param_knowledge_leader.md): no unconditional
                # perco moves — with deep_seep > 0, raising perco routes water
                # to deep-storage losses and collapses streamflow (verified in
                # a real run); probe perco individually before recommending it.
                if isinstance(lr, (int, float)) and lr < 0.9:
                    if "flo_min" not in locked:
                        adjustments.append({"param": "flo_min", "direction": "increase", "magnitude": "small", "reasoning": "Low flow too low: increase flo_min for baseflow"})
                    if "deep_seep" not in locked:
                        adjustments.append({"param": "deep_seep", "direction": "decrease", "magnitude": "small", "reasoning": "Low flow too low: keep aquifer recharge in the stream"})
                elif isinstance(lr, (int, float)) and lr > 1.1:
                    if "flo_min" not in locked:
                        adjustments.append({"param": "flo_min", "direction": "decrease", "magnitude": "small", "reasoning": "Low flow too high: decrease flo_min to reduce baseflow"})
                    if "deep_seep" not in locked:
                        adjustments.append({"param": "deep_seep", "direction": "increase", "magnitude": "small", "reasoning": "Low flow too high: divert recharge to deep storage"})
            else:
                # VIC-based combinations share the soil-parameter heuristics.
                if self.model_combination == "vic_cama_flood":
                    param_menu = list(SOIL_PARAM_KEYS) + list(CAMA_PARAM_KEYS)
                else:
                    # vic_routing — identical to legacy behaviour.
                    param_menu = list(PARAM_KEYS)

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

                if self.model_combination == "vic_cama_flood":
                    # Channel menu (prompts/vic_cama_flood/param_knowledge_leader.md):
                    # Wc is the dominant storage lever; Hc drives wave speed.
                    if "Wc" not in locked:
                        if isinstance(pr, (int, float)) and pr > 1.05:
                            adjustments.append({"param": "Wc", "direction": "increase", "magnitude": "small", "reasoning": "Peak too high: wider channels attenuate peaks"})
                        elif isinstance(pr, (int, float)) and pr < 0.95:
                            adjustments.append({"param": "Wc", "direction": "decrease", "magnitude": "small", "reasoning": "Peak too low: narrower channels are flashier"})
                    pt = m.get("peak_timing_bias_days")
                    if "Hc" not in locked and isinstance(pt, (int, float)) and math.isfinite(pt):
                        if pt < -0.5:
                            adjustments.append({"param": "Hc", "direction": "increase", "magnitude": "small", "reasoning": "Peaks early: deeper channels slow the wave"})
                        elif pt > 0.5:
                            adjustments.append({"param": "Hc", "direction": "decrease", "magnitude": "small", "reasoning": "Peaks late: shallower channels speed the wave"})

            # Fill remaining params with "hold"
            adj_params = {a["param"] for a in adjustments}
            for pk in param_menu:
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
