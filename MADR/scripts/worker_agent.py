#!/usr/bin/env python3
"""Worker parameter proposals from numerical evidence, history and Leader guidance.

The configured retry and fallback policy handles failed API calls.
"""

from __future__ import annotations
from .prompt_source import PromptSource
from .context_delivery import (validate_context_delivery_protocol, render_context_system, build_worker_prompt)
from .prompt_visibility import validate_prompt_visibility_protocol, visibility_metadata
from .diagnostic_evidence import METRIC_KEYS, recession_status

import json
from copy import deepcopy
import logging
from typing import Any, Dict, List, Optional, Tuple

from .llm_client import LLMClient
from .llm_failure_policy import is_historical_fallback, record_historical_recovery, call_usage
from .param_validator import (
    DEFAULT_RANGES,
    enforce_phase_locks,
    normalize_params,
    clamp,
)
from .worker_ledger import WorkerLedger
from .worker_state import WorkerExecutor
from .worker_diagnostics import diagnose_for_policy, DiagnosisResult

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

    def format_full_history_table(self, phase: str = "VIC_ONLY", exclude_latest_mc: bool = False) -> str:
        """Format retained round history as a compact table.

        Includes params + key metrics so the LLM can see full trajectory.
        """
        if not self.history:
            return "(no history yet)"

        import re
        phase = str(getattr(phase, "value", phase)).upper()
        is_routing = phase == "ROUTING_ONLY"
        is_joint = phase == "JOINT"

        # Header
        identity_header = "R|phase|execution|upstream"
        if is_joint:
            header = identity_header + "|binfilt|Ds|Dsmax|Ws|d2|d3|vel|diff|NSE|PBIAS|peak_r|lowflow|timing|note"
        elif is_routing:
            header = identity_header + "|vel|diff|NSE|PBIAS|peak_r|timing|note"
        else:
            header = identity_header + "|binfilt|Ds|Dsmax|Ws|d2|d3|NSE|PBIAS|peak_r|lowflow|note"

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

        lines = ["Accepted-state history. R is observation round; execution may be an earlier retained result. "
                 "histU labels compare upstream backgrounds within this table only; ? means not supplied, not zero.", header]
        upstream_labels = {}
        for entry in self.history:
            rid = entry["round_id"]
            p = entry.get("params", {})
            m = entry["metrics"]
            execution = entry.get("execution_identity") or {}
            execution_label = (f"{execution['run_id']}:{execution['candidate_id']}"
                               if 'run_id' in execution and 'candidate_id' in execution else '?')
            upstream = entry.get('upstream_context_id')
            if upstream and upstream not in upstream_labels:
                upstream_labels[upstream] = f"histU{len(upstream_labels) + 1}"
            identity = f"{rid}|{entry.get('phase', '?')}|{execution_label}|{upstream_labels.get(upstream, '?')}"

            nse_s = _f(m.get("NSE"), ".3f")
            pb_s = _f(m.get("PBIAS"), ".1f")
            pr_s = _f(m.get("peak_ratio"), ".2f")

            # MC annotation
            analysis = entry.get("worker_analysis", "")
            note = ""
            if "MC(" in analysis:
                m_mc = re.search(r'best=(\w+)', analysis)
                note = ("MC exact tie" if "exact_maximum_tie=" in analysis else
                        "MC scores unavailable" if "all_scores_unavailable" in analysis else
                        m_mc.group(1) + "✓" if m_mc else "MC")
            elif rid == 1:
                note = "default"

            if is_routing or is_joint:
                vel = _f(p.get("velocity"), ".2f")
                diff = _f(p.get("diffusivity"), ".0f")
                t_s = _f(m.get("peak_timing_bias_days"), ".1f")
                if is_joint:
                    soils = '|'.join(_f(p.get(key), '.3f') for key in
                                     ('binfilt', 'Ds', 'Dsmax', 'Ws', 'soil_d2', 'soil_d3'))
                    lines.append(f"{identity}|{soils}|{vel}|{diff}|{nse_s}|{pb_s}|{pr_s}|{_f(m.get('lowflow_ratio'), '.1f')}|{t_s}|{note}")
                else:
                    lines.append(f"{identity}|{vel}|{diff}|{nse_s}|{pb_s}|{pr_s}|{t_s}|{note}")
            else:
                lf_s = _f(m.get("lowflow_ratio"), ".1f")
                lines.append(
                    f"{identity}|{_f(p.get('binfilt'), '.3f')}|{_f(p.get('Ds'), '.3f')}|"
                    f"{_f(p.get('Dsmax'), '.3f')}|{_f(p.get('Ws'), '.3f')}|"
                    f"{_f(p.get('soil_d2'), '.3f')}|{_f(p.get('soil_d3'), '.3f')}|"
                    f"{nse_s}|{pb_s}|{pr_s}|{lf_s}|{note}"
                )
        from .phase_mc_feedback import compact_mc_history
        retained = self.recent
        if exclude_latest_mc and retained:
            retained = retained[:-1]
        mc_history = compact_mc_history([entry.get("experiment_feedback") for entry in retained])
        if mc_history:
            lines.append("Recent MC attempts (including unselected candidates): " + mc_history)
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
    """LLM-based parameter proposals and diagnostic reports for one sub-basin.

Execution enforces configured bounds and phase locks. Invalid or unavailable
LLM replies follow the configured failure policy."""

    def __init__(
        self,
        agent_id: str,
        subbasin_id: str,
        station_code: str,
        llm_client: Optional[LLMClient] = None,
        param_ranges: Optional[Dict[str, list]] = None,
        window_size: int = 5,
        static_input_protocol=None,
        worker_evidence_mode="existing",
        prompt_visibility_protocol=None,
        context_delivery_protocol=None,
        **kwargs,
    ):
        self.agent_id = agent_id
        self.subbasin_id = subbasin_id
        self.station_code = station_code
        self.llm = llm_client

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
        self.diagnostic_mode = "historical_policy"
        self.last_proposal_trace = None
        self.last_call_id = None
        self._trace_output_dir = None
        self.process_trace_enabled = True

        from .execution_protocol import validate_worker_evidence_mode
        self.worker_evidence_mode = validate_worker_evidence_mode(worker_evidence_mode)
        self.current_attribution_evidence = None
        self.prompt_visibility_protocol = validate_prompt_visibility_protocol(prompt_visibility_protocol)
        self.context_delivery_protocol = validate_context_delivery_protocol(context_delivery_protocol)
        self.configure_static_input(static_input_protocol)

    def configure_static_input(self, protocol=None):
        """Use the same authored text for direct and MC requests."""
        self.prompt_source = PromptSource(protocol)
        self.static_input_protocol = dict(self.prompt_source.protocol)
        self.system_prompt = render_context_system(self.prompt_source, "worker", self.prompt_visibility_protocol, self.context_delivery_protocol)
        self.propose_template = self.prompt_source.get_text("worker_user")
        self.static_prompt_metadata = self.prompt_source.provenance_metadata(
            system=self.system_prompt, user_template=self.propose_template)
        self.static_prompt_metadata.update(visibility_metadata(self.prompt_visibility_protocol))

    # ------------------------------------------------------------------
    # LLM parameter proposal
    # ------------------------------------------------------------------

    def propose_params(
        self,
        round_id: int,
        current_center: Dict[str, float],
        metrics: Dict[str, float],
        phase: str = "joint",
        leader_context: str = "",
    ) -> Tuple[List[Dict[str, float]], str, Dict[str, int]]:
        """Use Worker LLM to propose direct parameter values.

        No step decay — LLM decides adjustment magnitude autonomously.

        Parameters
        ----------
        round_id : int
            Current round number
        current_center : dict
            Current accepted parameter center
        metrics : dict
            Supplied measured hydrologic metrics, with provenance in the prompt
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
        from .process_trace import record_worker_proposal, snapshot
        self.last_proposal_trace = None
        self.last_call_id = None
        trace = {"round_id": round_id, "phase": phase, "branch": "llm_proposal",
                 "current_center": snapshot(current_center),
                 "leader_context_received": leader_context, "call_id": None}
        if self.llm is None:
            return self._recover_proposal(trace, current_center, phase, round_id, 'no_llm_available')

        # Build prompt
        prompt = self._build_iteration_prompt(
            round_id=round_id,
            current_center=current_center,
            metrics=metrics,
            phase=phase,
            leader_context=leader_context,
        )

        try:
            # Metadata is consumed by the recorder only; never injected into prompts.
            if hasattr(self.llm, "trace_metadata"):
                self.llm.trace_metadata = {"role": "worker", "round_id": round_id,
                                          "subbasin_id": self.subbasin_id, "phase": phase}
            # Single LLM call, no conversation history
            llm_resp = self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system=self.system_prompt,
            )

            # Capture token usage from LLM response
            token_usage = call_usage(self, llm_resp)
            trace['call_usage'] = getattr(self.llm, 'last_usage_summary', None)

            trace["call_id"] = getattr(self.llm, "last_call_id", None)
            trace["attempt_ids"] = list(getattr(self.llm, "last_attempt_ids", []))
            data = llm_resp.parsed
            trace["parsed_before_worker_repair"] = snapshot(data)
            if data is None:
                # Secondary repair: fix truncated numbers in the response
                raw = llm_resp.content or ""
                data = self._repair_truncated_json(raw)
                if data is None:
                    logger.warning(
                        "Worker %s LLM returned non-JSON: %s. Applying configured failure policy.",
                        self.subbasin_id,
                        raw[:300],
                    )
                    return self._recover_proposal(trace, current_center, phase, round_id,
                                                  'unparsed_after_worker_repair')
                logger.info("Worker %s JSON repaired successfully via secondary parser.", self.subbasin_id)

            # Extract direct parameter values from LLM output
            vic_changes = data.get("vic", {})
            routing_changes = data.get("routing", {})
            reasoning = data.get("reasoning", "LLM proposal")
            trace["raw_parsed_proposal"] = snapshot(data)
            trace["provided_evidence_refs"] = list(getattr(self, "experiment_evidence_refs", []))
            trace["reasoning"] = reasoning
            raw_merged = dict(current_center)
            for changes in (vic_changes, routing_changes):
                raw_merged.update({k: v for k, v in changes.items() if k in self.ranges})
            trace["merged_before_clamp"] = snapshot(raw_merged)

            # Build full parameter dict: start from best, apply LLM changes
            candidate = dict(current_center)
            for param, value in vic_changes.items():
                if param in self.ranges:
                    lo, hi = self.ranges[param]
                    candidate[param] = clamp(value, lo, hi)
            for param, value in routing_changes.items():
                if param in self.ranges:
                    lo, hi = self.ranges[param]
                    candidate[param] = clamp(value, lo, hi)

            trace["after_clamp_before_phase_locks"] = snapshot(candidate)
            # Enforce phase locks (enforce_phase_locks expects List[Dict])
            locked_list = enforce_phase_locks([candidate], phase, current_center)
            candidate = locked_list[0]
            trace["after_phase_locks_before_normalize"] = snapshot(candidate)

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

            record_worker_proposal(self, {**trace, "normalized_candidates": [candidate],
                                         "fallback_reason": None})
            return [candidate], f"LLM: {reasoning}", token_usage

        except Exception as e:
            logger.error("Worker %s LLM proposal unavailable (%s); applying declared policy.",
                         self.subbasin_id, type(e).__name__)
            return self._recover_proposal(trace, current_center, phase, round_id,
                                          'failed_worker_proposal', error=e)

    def _recover_proposal(self, trace, center, phase, round_id, reason, error=None):
        from .process_trace import record_worker_proposal, snapshot
        historical = is_historical_fallback(self)
        recovery = record_historical_recovery(self, reason, round_id=round_id, phase=phase, error=error)
        candidates = self._fallback_execute(center, phase)
        branch = 'historical_rule_fallback' if historical else 'neutral_fallback'
        explanation = ('Historical code fallback: diagnosis-based steps and perturbations; no LLM proposal.'
                       if historical else 'Neutral code fallback repeats the current center; no LLM proposal.')
        record_worker_proposal(self, {**trace, 'call_id': getattr(self.llm, 'last_call_id', None),
            'attempt_ids': list(getattr(self.llm, 'last_attempt_ids', [])),
            'branch': branch, 'fallback_reason': reason,
            'failure_class': (recovery or {}).get('failure_class'),
            'recovery': recovery, 'call_usage': getattr(self.llm, 'last_usage_summary', None),
            'fallback_instruction': snapshot(getattr(self, '_fallback_instruction', None)),
            'fallback_execution': snapshot(getattr(self.executor, 'last_instruction_trace', None)),
            'normalized_candidates': candidates, 'reasoning': explanation})
        return candidates, explanation, call_usage(self)

    def _build_iteration_prompt(
        self,
        round_id: int,
        current_center: Dict[str, float],
        metrics: Dict[str, float],
        phase: str,
        leader_context: str = "",
    ) -> str:
        """Build a user prompt from the configured projection and measured history."""
        return build_worker_prompt(self, round_id, current_center, metrics, phase, leader_context)

    @staticmethod
    def _repair_truncated_json(raw: str) -> Optional[Dict[str, Any]]:
        """Secondary JSON repair for truncated model outputs.

        Handles JSON with:
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
        """Diagnosis-based step/noise fallback, or an explicit neutral fallback."""
        self._fallback_instruction = None
        if not is_historical_fallback(self):
            return [dict(current_center)]
        instruction = {'param_adjustments': [], 'current_center': dict(current_center)}
        from .current_evidence import proposal_diagnosis
        fallback_diagnosis = proposal_diagnosis(self)
        if fallback_diagnosis and fallback_diagnosis.recommendations:
            for rec in fallback_diagnosis.recommendations[:3]:
                instruction['param_adjustments'].append({
                    'param': rec.get('param', 'binfilt'),
                    'direction': rec.get('direction', 'hold'),
                    'magnitude': rec.get('magnitude', 'small')})
        self._fallback_instruction = instruction
        candidates = self.executor.generate_candidates(base_params=dict(current_center),
            leader_instruction=instruction, phase=phase)
        if len(candidates) != 1:
            raise RuntimeError('Historical fallback must produce exactly one candidate per scheduled slot')
        locked = enforce_phase_locks(candidates, phase, current_center)
        return [normalize_params(candidate, self.ranges) for candidate in locked]

    # ------------------------------------------------------------------
    # Diagnostics and round records
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
        diagnosis = diagnose_for_policy(self.diagnostic_mode, metrics, prev_metrics, context)
        self._last_diagnosis = diagnosis
        self._last_simulation_ok = (context or {}).get("simulation_ok")
        # Preserve the evaluated record that the cached labels actually describe.
        self._latest_evaluation_record = {
            "round_id": (context or {}).get("round_id"),
            "params": deepcopy((context or {}).get("evaluated_params")),
            "metrics": deepcopy(metrics), "ok": self._last_simulation_ok,
        }
        context = context or {}
        accepted_record = context.get('accepted_evaluation_record')
        if isinstance(accepted_record, dict):
            from .factual_context import leader_accepted_metric_record
            actual = leader_accepted_metric_record(accepted_record, context.get('round_id'),
                                                   context.get('evaluated_params'))
            self._latest_evaluation_record.update({key: deepcopy(actual[key]) for key in (
                'round_id', 'observed_round_id', 'evaluation_round_id', 'execution_identity',
                'upstream_context_id', 'accepted_record_source', 'result_scope', 'provenance_status')
                if key in actual})
        self._latest_evaluation_record['phase'] = context.get('observed_phase')
        self._latest_evaluation_record['observed_round_id'] = context.get('round_id')

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
                # Deterministic classification and availability must reach the Leader:
                # without these the evidence block silently drops its symptom line.
                "issue_status": diagnosis.issue_status,
                "diagnostic_status": diagnosis.diagnostic_status,
                "unavailable_metrics": diagnosis.unavailable_metrics,
                "recommendations": diagnosis.recommendations,
                "rule_conflicts": diagnosis.rule_conflicts,
                "attribution_kind": diagnosis.attribution_kind,
                "rule_audit": getattr(diagnosis, "rule_audit", {}),
            },
            "current_metrics": {k: metrics.get(k) for k in METRIC_KEYS + (
                "diagnostic_status", "evaluation_start", "evaluation_end", "n_scored_days")},
            "ledger_summary": ledger_summary,
            "diagnostic_evaluation_record": deepcopy(self._latest_evaluation_record),
            "stagnation_scope": {
                "through_round_id": max(0, (context.get('round_id') or 1) - 1),
                "criterion": "no NSE exceeding historical best + 0.001; not rejected-candidate count",
            },
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

        recession_label = recession_status(metrics.get("recession_k_ratio"))

        return {
            "nse": round(nse, 4) if nse not in (-999, None) else None,
            "nse_delta": nse_delta,
            "primary_issue": diagnosis.primary_issue if diagnosis else "none",
            "timing_status": timing_status,
            "recession_status": recession_label,
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
        ok: bool = True,
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
        # Diagnostic report describes this round's accepted measured state, not
        # necessarily the proposal selected before acceptance. Keep native IDs.
        entry = self.memory.history[-1]
        entry['phase'] = str(getattr(phase, 'value', phase)).upper()
        evaluated = getattr(self, '_latest_evaluation_record', {}) or {}
        if evaluated.get('observed_round_id', evaluated.get('round_id')) == round_id:
            for key in ('execution_identity', 'upstream_context_id', 'evaluation_round_id',
                        'accepted_record_source', 'result_scope', 'provenance_status'):
                if key in evaluated:
                    entry[key] = deepcopy(evaluated[key])

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
                status="success" if ok else "failed",
                error=None if ok else "model_or_evaluation_failed",
            )
        except Exception as e:
            logger.warning("Worker %s ledger append failed: %s", self.subbasin_id, e)

    def set_output_dir(self, output_dir: str):
        """Set output directory (called by orchestrator)."""
        self.output_dir = output_dir
        self._trace_output_dir = output_dir
        self.ledger = WorkerLedger(output_dir=output_dir, subbasin_id=self.subbasin_id)
