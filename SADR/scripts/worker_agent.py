#!/usr/bin/env python3
"""Worker Agent: pure-code sub-basin calibrator (CRAv2 — Leader-Only).

Workers execute Leader's structured instructions (direction + magnitude)
via WorkerExecutor lookup table. No Worker LLM calls.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    clamp,
)
from .worker_ledger import WorkerLedger
from .worker_state import WorkerExecutor
from .worker_diagnostics import diagnose_round, DiagnosisResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker memory (sliding window)
# ---------------------------------------------------------------------------

class WorkerMemory:
    """Sliding-window history for a worker agent."""

    def __init__(self, subbasin_id: str, station_code: str, window_size: int = 5):
        self.subbasin_id = subbasin_id
        self.station_code = station_code
        self.window_size = window_size
        self.history: List[Dict[str, Any]] = []

    @property
    def recent(self) -> List[Dict[str, Any]]:
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

    def format_compressed_history(self) -> str:
        if not self.history:
            return "  (first iteration)"
        lines = []
        for entry in self.recent:
            rid = entry["round_id"]
            m = entry["metrics"]
            nse = m.get("NSE")
            pbias = m.get("PBIAS")
            nse_str = f"{nse:.4f}" if isinstance(nse, (int, float)) else "?"
            pbias_str = f"{pbias:.1f}" if isinstance(pbias, (int, float)) else "?"
            ptb = m.get("peak_timing_bias_days")
            ptb_str = f"{ptb:.1f}" if isinstance(ptb, (int, float)) else "N/A"
            reason = entry.get("worker_analysis", "")[:80]
            lines.append(
                f"  R{rid}: NSE={nse_str}, PBIAS={pbias_str}%, "
                f"peak_timing={ptb_str}d | {reason}"
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
# Worker Agent (CRAv2: pure code, no LLM)
# ---------------------------------------------------------------------------

class WorkerAgent:
    """Pure-code worker agent for one sub-basin (CRAv2 Leader-Only).

    Receives Leader's structured instruction (direction + magnitude per param)
    and converts to concrete parameter values via WorkerExecutor lookup table.
    """

    def __init__(
        self,
        agent_id: str,
        subbasin_id: str,
        station_code: str,
        is_outlet: bool = False,
        neighbors_downstream: Optional[List[str]] = None,
        param_ranges: Optional[Dict[str, list]] = None,
        window_size: int = 5,
        **kwargs,
    ):
        self.agent_id = agent_id
        self.subbasin_id = subbasin_id
        self.station_code = station_code
        self.is_outlet = is_outlet
        self.neighbors_downstream = neighbors_downstream or []

        self.ranges: Dict[str, Tuple[float, float]] = {}
        if param_ranges:
            for k, v in param_ranges.items():
                self.ranges[k] = (float(v[0]), float(v[1]))
        else:
            self.ranges = dict(DEFAULT_RANGES)

        self.memory = WorkerMemory(subbasin_id, station_code, window_size)

        self.output_dir = "output"
        self.ledger = WorkerLedger(output_dir=self.output_dir, subbasin_id=subbasin_id)
        self.executor = WorkerExecutor(param_ranges=self.ranges)
        self._last_diagnosis: Optional[DiagnosisResult] = None

    # ------------------------------------------------------------------
    # Code-based parameter execution
    # ------------------------------------------------------------------

    def execute_instruction(
        self,
        leader_instruction: Dict[str, Any],
        phase: str = "joint",
        radius: Optional[Dict[str, float]] = None,
        escape_mode: bool = False,
        decay_factor: float = 1.0,
    ) -> List[Dict[str, float]]:
        """Execute Leader instruction via code (WorkerExecutor lookup table).

        Converts direction + magnitude → concrete parameter step:
        - small: 10% of param range
        - medium: 22% of param range
        - large: 40% of param range

        Step is multiplied by decay_factor for progressive refinement.
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
    # Diagnostics, recording, output dir
    # ------------------------------------------------------------------

    def report_diagnostics(
        self,
        metrics: Dict[str, Any],
        prev_metrics: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run local diagnostics and generate report for Leader."""
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
