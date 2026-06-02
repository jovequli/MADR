#!/usr/bin/env python3
"""Worker Agent: LLM-driven sub-basin calibrator (cali5.2_LW).

Each worker uses LLM to propose DIRECT parameter values based on
diagnosis and history. Falls back to WorkerExecutor (code-based
lookup table) on LLM failure or during escape mode.

Design reference: ZoneAgent pattern (~2200 tokens per LLM call).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .llm_client import LLMClient, LLMResponse
from .param_validator import (
    PARAM_KEYS,
    SOIL_PARAM_KEYS,
    ROUTING_PARAM_KEYS,
    DEFAULT_RANGES,
    CalibrationPhase,
    get_tunable_keys,
    get_locked_keys,
    enforce_phase_locks,
    normalize_params,
    fallback_rule_based_candidates,
    clamp,
)
from .worker_ledger import WorkerLedger
from .worker_state import WorkerExecutor
from .worker_diagnostics import diagnose_round, DiagnosisResult

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Worker memory (sliding window)
# ---------------------------------------------------------------------------

class WorkerMemory:
    """Sliding-window history for a worker agent."""

    def __init__(self, subbasin_id: str, station_code: str, window_size: int = 5):
        self.subbasin_id = subbasin_id
        self.station_code = station_code
        self.window_size = window_size
        self.history: List[Dict[str, Any]] = []  # full history

    @property
    def recent(self) -> List[Dict[str, Any]]:
        """Return last N rounds (sliding window)."""
        return self.history[-self.window_size:]

    def add_round(
        self,
        round_id: int,
        params: Dict[str, float],
        metrics: Dict[str, float],
        score_local: float,
        leader_guidance: Optional[Dict] = None,
        worker_analysis: str = "",
    ):
        self.history.append({
            "round_id": round_id,
            "params": params,
            "metrics": metrics,
            "score_local": score_local,
            "leader_guidance": leader_guidance,
            "worker_analysis": worker_analysis,
        })

    def format_full_history_table(self, phase: str = "VIC_ONLY") -> str:
        """Format ALL rounds as a compact table.

        Header depends on phase:
        - VIC_ONLY: 6 soil params (routing locked at defaults)
        - ROUTING_ONLY: 2 routing params (soil locked at VIC's best)
        - JOINT: all 8 params (fine-tuning everything)
        """
        if not self.history:
            return "(no history yet)"

        import re
        phase_upper = phase.upper() if isinstance(phase, str) else str(phase)

        if phase_upper == "ROUTING_ONLY":
            header = "R|vel|diff|NSE|PBIAS|peak_r|timing|note"
        elif phase_upper == "JOINT":
            header = "R|binfilt|Ds|Dsmax|Ws|d2|d3|vel|diff|NSE|PBIAS|peak_r|lowflow|timing|note"
        else:
            header = "R|binfilt|Ds|Dsmax|Ws|d2|d3|NSE|PBIAS|peak_r|lowflow|note"

        def _f(v, fmt):
            """Safe float format, returns '?' for None/NaN."""
            if v is None:
                return "?"
            try:
                fv = float(v)
                if fv != fv:  # NaN
                    return "?"
                return format(fv, fmt)
            except (ValueError, TypeError):
                return "?"

        lines = [header]
        for entry in self.history:
            rid = entry["round_id"]
            p = entry.get("params", {})
            m = entry["metrics"]

            nse_s = _f(m.get("NSE"), ".3f")
            pb_s = _f(m.get("PBIAS"), ".1f")
            pr_s = _f(m.get("peak_ratio"), ".2f")

            # MC annotation
            analysis = entry.get("worker_analysis", "")
            note = ""
            if "MC(" in analysis:
                m_mc = re.search(r'best=(\w+)', analysis)
                note = m_mc.group(1) + "✓" if m_mc else "MC"
            elif rid == 1:
                note = "default"

            if phase_upper == "ROUTING_ONLY":
                vel = _f(p.get("velocity", 1.5), ".2f")
                diff = _f(p.get("diffusivity", 800), ".0f")
                t_s = _f(m.get("peak_timing_bias_days"), ".1f")
                lines.append(f"{rid}|{vel}|{diff}|{nse_s}|{pb_s}|{pr_s}|{t_s}|{note}")
            elif phase_upper == "JOINT":
                lf_s = _f(m.get("lowflow_ratio"), ".1f")
                t_s = _f(m.get("peak_timing_bias_days"), ".1f")
                lines.append(
                    f"{rid}|{_f(p.get('binfilt'), '.3f')}|{_f(p.get('Ds'), '.4f')}|"
                    f"{_f(p.get('Dsmax'), '.1f')}|{_f(p.get('Ws'), '.2f')}|"
                    f"{_f(p.get('soil_d2'), '.2f')}|{_f(p.get('soil_d3'), '.2f')}|"
                    f"{_f(p.get('velocity', 1.5), '.2f')}|{_f(p.get('diffusivity', 800), '.0f')}|"
                    f"{nse_s}|{pb_s}|{pr_s}|{lf_s}|{t_s}|{note}"
                )
            else:
                lf_s = _f(m.get("lowflow_ratio"), ".1f")
                lines.append(
                    f"{rid}|{_f(p.get('binfilt'), '.3f')}|{_f(p.get('Ds'), '.4f')}|"
                    f"{_f(p.get('Dsmax'), '.1f')}|{_f(p.get('Ws'), '.2f')}|"
                    f"{_f(p.get('soil_d2'), '.2f')}|{_f(p.get('soil_d3'), '.2f')}|"
                    f"{nse_s}|{pb_s}|{pr_s}|{lf_s}|{note}"
                )
        return "\n".join(lines)

    def to_checkpoint(self) -> Dict:
        return {
            "subbasin_id": self.subbasin_id,
            "station_code": self.station_code,
            "window_size": self.window_size,
            "history": self.history,
        }

    @classmethod
    def from_checkpoint(cls, data: Dict) -> "WorkerMemory":
        mem = cls(
            data["subbasin_id"],
            data["station_code"],
            data.get("window_size", 5),
        )
        mem.history = data.get("history", [])
        return mem


# ---------------------------------------------------------------------------
# Worker Agent
# ---------------------------------------------------------------------------

class WorkerAgent:
    """LLM-driven worker agent for one sub-basin (cali5.2_LW).

    In agent mode: Worker LLM proposes DIRECT parameter values.
    In escape mode: WorkerExecutor generates random perturbations (pure code).
    On LLM failure: Falls back to WorkerExecutor lookup table.
    """

    def __init__(
        self,
        agent_id: str,
        subbasin_id: str,
        station_code: str,
        llm_client: Optional[LLMClient] = None,
        is_outlet: bool = False,
        neighbors_downstream: Optional[List[str]] = None,
        param_ranges: Optional[Dict[str, list]] = None,
        window_size: int = 5,
        **kwargs,
    ):
        self.agent_id = agent_id
        self.subbasin_id = subbasin_id
        self.station_code = station_code
        self.llm = llm_client
        self.is_outlet = is_outlet
        self.neighbors_downstream = neighbors_downstream or []

        self.ranges: Dict[str, Tuple[float, float]] = {}
        if param_ranges:
            for k, v in param_ranges.items():
                self.ranges[k] = (float(v[0]), float(v[1]))
        else:
            self.ranges = dict(DEFAULT_RANGES)

        self.memory = WorkerMemory(subbasin_id, station_code, window_size)

        # Persistent storage + code executor (fallback / escape mode)
        self.output_dir = "output"
        self.ledger = WorkerLedger(output_dir=self.output_dir, subbasin_id=subbasin_id)
        self.executor = WorkerExecutor(param_ranges=self.ranges)
        self._last_diagnosis: Optional[DiagnosisResult] = None

        # Load compact prompts (ZoneAgent style)
        self.system_prompt = _load_text(PROMPTS_DIR / "worker_system.md")
        self.propose_template = _load_text(PROMPTS_DIR / "worker_propose.md")

    # ------------------------------------------------------------------
    # NEW: LLM-driven parameter proposal (ZoneAgent style)
    # ------------------------------------------------------------------

    def build_propose_prompt(
        self,
        round_id: int,
        current_center: Dict[str, float],
        metrics: Dict[str, float],
        phase: str = "joint",
        leader_context: str = "",
    ) -> Tuple[str, str]:
        """Build the proposal prompt without calling the LLM.

        Separates prompt construction from LLM invocation so that
        LangGraph nodes can manage the call independently.

        Returns:
            (user_prompt, system_prompt)
        """
        prompt = self._build_iteration_prompt(
            round_id=round_id,
            current_center=current_center,
            metrics=metrics,
            phase=phase,
            leader_context=leader_context,
        )
        return prompt, self.system_prompt

    def propose_params(
        self,
        round_id: int,
        current_center: Dict[str, float],
        metrics: Dict[str, float],
        phase: str = "joint",
        leader_context: str = "",
    ) -> Tuple[List[Dict[str, float]], str, Dict[str, int]]:
        """Use Worker LLM to propose direct parameter values.

        ~2200 tokens per call: system(~550) + user(~1450) + output(~170).
        No step decay — LLM decides adjustment magnitude autonomously.

        Parameters
        ----------
        round_id : int
            Current round number
        current_center : dict
            Current best parameters (revert-to-best base)
        metrics : dict
            Current round's hydrologic metrics
        phase : str
            Calibration phase: "VIC_ONLY", "ROUTING_ONLY", "JOINT"
        leader_context : str
            Brief summary from Leader's analysis for this station

        Returns
        -------
        (candidates, reasoning, token_usage) : Tuple[List[Dict], str, Dict]
            candidates: list with 1 full parameter dict
            reasoning: LLM's reasoning string
            token_usage: {"prompt": int, "completion": int, "total": int}
        """
        _zero_tokens: Dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}
        if self.llm is None:
            logger.warning("Worker %s has no LLM, falling back to executor", self.subbasin_id)
            return self._fallback_execute(current_center, phase), "no LLM available", _zero_tokens

        # Build prompt
        prompt = self._build_iteration_prompt(
            round_id=round_id,
            current_center=current_center,
            metrics=metrics,
            phase=phase,
            leader_context=leader_context,
        )

        try:
            # Single LLM call, no conversation history
            llm_resp = self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system=self.system_prompt,
            )

            # Capture token usage from LLM response
            token_usage = {
                "prompt": llm_resp.prompt_tokens,
                "completion": llm_resp.completion_tokens,
                "total": llm_resp.total_tokens,
            }

            data = llm_resp.parsed
            if data is None:
                # Secondary repair: try to fix truncated numbers from Flash models
                raw = llm_resp.content or ""
                data = self._repair_flash_json(raw)
                if data is None:
                    logger.warning(
                        "Worker %s LLM returned non-JSON: %s. Using executor fallback.",
                        self.subbasin_id,
                        raw[:300],
                    )
                    return self._fallback_execute(current_center, phase), "LLM non-JSON fallback", token_usage
                logger.info("Worker %s JSON repaired successfully via secondary parser.", self.subbasin_id)

            # Extract direct parameter values from LLM output
            vic_changes = data.get("vic", {})
            routing_changes = data.get("routing", {})
            reasoning = data.get("reasoning", "LLM proposal")

            # Build full parameter dict: start from best, apply LLM changes
            candidate = dict(current_center)
            for param, value in vic_changes.items():
                if param in self.ranges:
                    lo, hi = self.ranges[param]
                    candidate[param] = clamp(float(value), lo, hi)
            for param, value in routing_changes.items():
                if param in self.ranges:
                    lo, hi = self.ranges[param]
                    candidate[param] = clamp(float(value), lo, hi)

            # Enforce phase locks (enforce_phase_locks expects List[Dict])
            locked_list = enforce_phase_locks([candidate], phase, current_center)
            candidate = locked_list[0]

            # Enforce hard constraints: soil_d3 > soil_d2
            if candidate.get("soil_d3", 1.0) <= candidate.get("soil_d2", 0.3):
                candidate["soil_d3"] = candidate["soil_d2"] + 0.01

            # Normalize
            candidate = normalize_params(candidate, self.ranges)

            logger.info(
                "Worker %s LLM proposed: vic=%s routing=%s | tokens=%s | %s",
                self.subbasin_id,
                {k: v for k, v in vic_changes.items()},
                {k: v for k, v in routing_changes.items()},
                token_usage.get("total", 0),
                reasoning[:100],
            )

            return [candidate], f"LLM: {reasoning}", token_usage

        except Exception as e:
            logger.error("Worker %s LLM call failed: %s. Using executor fallback.", self.subbasin_id, e)
            return self._fallback_execute(current_center, phase), f"LLM error fallback: {e}", _zero_tokens

    def _build_iteration_prompt(
        self,
        round_id: int,
        current_center: Dict[str, float],
        metrics: Dict[str, float],
        phase: str,
        leader_context: str = "",
    ) -> str:
        """Build compact user prompt with full history table.

        V3 design: full history table replaces sliding-window summary.
        ~25 tokens/row × N rounds + fixed overhead ~200 tokens.
        """
        import math
        phase_upper = phase.upper() if isinstance(phase, str) else str(phase)

        def _safe(v, fmt=".4f"):
            if v is None:
                return "N/A"
            try:
                f = float(v)
                if math.isnan(f) or math.isinf(f):
                    return "N/A"
                return format(f, fmt)
            except (ValueError, TypeError):
                return "N/A"

        # ── Diagnosis block (~60 tokens) ──
        diagnosis = self._last_diagnosis
        if diagnosis:
            diag_block = (
                f"Issue: {diagnosis.primary_issue} ({diagnosis.severity}), "
                f"VIC={diagnosis.vic_responsibility:.0%} Rout={diagnosis.routing_responsibility:.0%}\n"
                f"{diagnosis.reasoning[:120]}"
            )
        else:
            diag_block = "First round — no diagnosis."

        # ── Full history table (~25 tokens/row) ──
        history_table = self.memory.format_full_history_table(phase=phase_upper)

        # ── Best NSE ──
        best_record = self.ledger.get_best_params()
        best_nse = best_record.get("nse", 0.0) if best_record else 0.0
        best_nse_str = f"{best_nse:.4f}" if isinstance(best_nse, (int, float)) else "0.0000"

        # ── Leader guidance ──
        leader_block = ""
        if leader_context:
            leader_block = f"\nLEADER: {leader_context}"

        # ── Exploration reference (injected every round) ──
        explore_block = ""
        if hasattr(self, '_exploration_summary') and self._exploration_summary:
            explore_block = f"\nEXPLORATION INSIGHT:\n{self._exploration_summary}"

        # ── Bounds ──
        if phase_upper == "VIC_ONLY":
            bounds = {k: list(v) for k, v in self.ranges.items() if k in SOIL_PARAM_KEYS}
            frozen_note = "Frozen: velocity, diffusivity"
        elif phase_upper == "ROUTING_ONLY":
            bounds = {k: list(v) for k, v in self.ranges.items() if k in ROUTING_PARAM_KEYS}
            frozen_note = "Frozen: binfilt, Ds, Dsmax, Ws, soil_d2, soil_d3"
        else:
            bounds = {k: list(v) for k, v in self.ranges.items()}
            frozen_note = ""

        return self.propose_template.format(
            subbasin_id=self.subbasin_id,
            station_code=self.station_code,
            phase=phase_upper,
            best_nse=best_nse_str,
            diagnosis_block=diag_block,
            history_table=history_table,
            leader_block=leader_block,
            explore_block=explore_block,
            bounds=json.dumps(bounds),
            frozen_note=frozen_note,
        )

    @staticmethod
    def _repair_flash_json(raw: str) -> Optional[Dict[str, Any]]:
        """Secondary JSON repair for Gemini Flash truncated outputs.

        Flash models sometimes produce JSON with:
        - Truncated decimal numbers: "Ds": 0.01. → "Ds": 0.01
        - Trailing dots on numbers: 8.0,. → 8.0
        - Missing closing braces
        - Markdown code block wrapping
        """
        import re

        text = raw.strip()

        # Remove markdown code block if present
        if text.startswith("```"):
            # Remove opening ```json or ```
            text = re.sub(r'^```(?:json)?\s*\n?', '', text)
            text = re.sub(r'\n?```\s*$', '', text)
            text = text.strip()

        # Find outermost { ... } region
        brace_start = text.find("{")
        if brace_start < 0:
            return None
        text = text[brace_start:]

        # Fix truncated numbers: trailing dots like "0.01." → "0.01"
        text = re.sub(r'(\d+\.\d+)\.(\s*[,}\]])', r'\1\2', text)
        # Fix "0." at end of truncated values → "0.0"
        text = re.sub(r': (\d+)\.\s*([,}\]])', r': \1.0\2', text)
        # Fix double dots: "8.0,." → "8.0,"
        text = re.sub(r',\.\s', ', ', text)
        # Remove trailing commas before } or ]
        text = re.sub(r',\s*([}\]])', r'\1', text)

        # Try parsing directly
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try closing open braces
        open_braces = text.count('{') - text.count('}')
        open_brackets = text.count('[') - text.count(']')

        # If there's a trailing incomplete value (e.g., "Ds": 0.0), try to close
        # Remove trailing incomplete key-value pairs
        text_trimmed = re.sub(r',\s*"[^"]*"\s*:\s*[^,}\]]*$', '', text)

        suffix = '}' * max(0, open_braces) + ']' * max(0, open_brackets)
        for attempt in (text + suffix, text_trimmed + suffix):
            # Clean up again after trimming
            attempt = re.sub(r',\s*([}\]])', r'\1', attempt)
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                pass

        return None

    def _fallback_execute(
        self,
        current_center: Dict[str, float],
        phase: str,
    ) -> List[Dict[str, float]]:
        """Fallback to WorkerExecutor when LLM fails.

        Uses diagnosis-based heuristic to generate direction/magnitude,
        then feeds to executor. No step decay in cali5.2_LW.
        """
        # Build a simple instruction from last diagnosis
        instruction = {"param_adjustments": [], "current_center": current_center}
        if self._last_diagnosis and self._last_diagnosis.recommendations:
            for rec in self._last_diagnosis.recommendations[:3]:
                instruction["param_adjustments"].append({
                    "param": rec.get("param", "binfilt"),
                    "direction": rec.get("direction", "hold"),
                    "magnitude": rec.get("magnitude", "small"),
                })

        return self.executor.generate_candidates(
            base_params=current_center,
            leader_instruction=instruction,
            phase=phase,
        )

    # ------------------------------------------------------------------
    # Kept: execute_instruction (for escape mode, backward compat)
    # ------------------------------------------------------------------

    def execute_instruction(
        self,
        leader_instruction: Dict[str, Any],
        phase: str = "joint",
        radius: Optional[Dict[str, float]] = None,
        escape_mode: bool = False,
        decay_factor: float = 1.0,
    ) -> List[Dict[str, float]]:
        """Execute Leader instruction via code (WorkerExecutor).

        Used for:
        - Escape mode (random perturbation, LLM skipped)
        - Backward compatibility
        """
        best_record = self.ledger.get_best_params()
        if best_record and best_record.get("params"):
            base_params = dict(best_record["params"])
        else:
            base_params = leader_instruction.get("current_center", {})
            if not base_params:
                base_params = {
                    k: (lo + hi) / 2 for k, (lo, hi) in self.ranges.items()
                }

        return self.executor.generate_candidates(
            base_params=base_params,
            leader_instruction=leader_instruction,
            phase=phase,
            radius=radius,
            escape_mode=escape_mode,
            decay_factor=decay_factor,
        )

    # ------------------------------------------------------------------
    # Kept: report_diagnostics, record_round, set_output_dir
    # ------------------------------------------------------------------

    def report_diagnostics(
        self,
        metrics: Dict[str, Any],
        prev_metrics: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run local diagnostics and generate report for Leader.

        Diagnosis is pre-computed by code — also cached for Worker LLM prompt.
        """
        diagnosis = diagnose_round(metrics, prev_metrics, context)
        self._last_diagnosis = diagnosis

        ledger_summary = self.ledger.summary()
        best_record = self.ledger.get_best_params()

        return {
            "subbasin_id": self.subbasin_id,
            "station_code": self.station_code,
            "diagnosis": {
                "primary_issue": diagnosis.primary_issue,
                "severity": diagnosis.severity,
                "severity_score": diagnosis.severity_score,
                "vic_responsibility": diagnosis.vic_responsibility,
                "routing_responsibility": diagnosis.routing_responsibility,
                "upstream_responsibility": diagnosis.upstream_responsibility,
                "reasoning": diagnosis.reasoning,
                "improvement_trend": diagnosis.improvement_trend,
                "recommendations": diagnosis.recommendations,
            },
            "current_metrics": {
                "NSE": metrics.get("NSE"),
                "PBIAS": metrics.get("PBIAS"),
                "peak_ratio": metrics.get("peak_ratio"),
                "lowflow_ratio": metrics.get("lowflow_ratio"),
                "peak_timing_bias_days": metrics.get("peak_timing_bias_days"),
                "recession_k_ratio": metrics.get("recession_k_ratio"),
                "baseflow_index_bias_pct": metrics.get("baseflow_index_bias_pct"),
                "KGE": metrics.get("KGE"),
            },
            "ledger_summary": ledger_summary,
            "best_params": best_record.get("params") if best_record else None,
            "best_nse": best_record.get("nse") if best_record else None,
            "round_summary": self._build_round_summary(metrics, prev_metrics, diagnosis, ledger_summary),
        }

    def _build_round_summary(
        self,
        metrics: Dict[str, Any],
        prev_metrics: Optional[Dict[str, Any]],
        diagnosis,
        ledger_summary: Dict,
    ) -> Dict[str, Any]:
        """Build concise round summary for Leader quick overview."""
        import math
        def _to_float(v, default=-999):
            if v is None:
                return default
            try:
                f = float(v)
                return default if math.isnan(f) or math.isinf(f) else f
            except (ValueError, TypeError):
                return default

        nse = _to_float(metrics.get("NSE"))
        prev_nse = _to_float(prev_metrics.get("NSE")) if prev_metrics else None
        if prev_nse is not None and nse != -999 and prev_nse != -999:
            nse_delta = round(nse - prev_nse, 4)
        else:
            nse_delta = None

        ptb = _to_float(metrics.get("peak_timing_bias_days"), None)
        if ptb is not None:
            timing_status = "early" if ptb < -2 else ("late" if ptb > 2 else "ok")
        else:
            timing_status = "unknown"

        rk = _to_float(metrics.get("recession_k_ratio"), None)
        if rk is not None:
            recession_status = "too_fast" if rk < 0.8 else ("too_slow" if rk > 1.2 else "ok")
        else:
            recession_status = "unknown"

        return {
            "nse": round(nse, 4) if nse not in (-999, None) else None,
            "nse_delta": nse_delta,
            "primary_issue": diagnosis.primary_issue if diagnosis else "none",
            "timing_status": timing_status,
            "recession_status": recession_status,
            "is_stagnant": ledger_summary.get("stagnation_count", 0) >= 3,
        }

    def record_round(
        self,
        round_id: int,
        best_params: Dict[str, float],
        metrics: Dict[str, float],
        score_local: float,
        leader_guidance: Optional[Dict] = None,
        analysis: str = "",
        phase: str = "joint",
        escape_label: str = "",
    ):
        """Record this round's results in worker memory + ledger."""
        self.memory.add_round(
            round_id=round_id,
            params=best_params,
            metrics=metrics,
            score_local=score_local,
            leader_guidance=leader_guidance,
            worker_analysis=analysis,
        )

        diagnosis_summary = ""
        if self._last_diagnosis:
            diagnosis_summary = self._last_diagnosis.summary

        leader_guidance_str = ""
        if leader_guidance:
            leader_guidance_str = leader_guidance.get("priority_focus", "")

        try:
            self.ledger.append(
                round_id=round_id,
                phase=phase,
                params=best_params,
                metrics=metrics,
                diagnosis_summary=diagnosis_summary,
                reasoning=analysis,
                selected_action="",
                leader_guidance=leader_guidance_str,
                escape_label=escape_label,
            )
        except Exception as e:
            logger.warning("Worker %s ledger append failed: %s", self.subbasin_id, e)

    def set_output_dir(self, output_dir: str):
        """Set output directory (called by orchestrator)."""
        self.output_dir = output_dir
        self.ledger = WorkerLedger(output_dir=output_dir, subbasin_id=self.subbasin_id)
