"""Diagnosis record and dispatch to the configured diagnostic policy.

Symptoms do not uniquely identify physical causes. Missing or invalid metrics
do not support a corresponding diagnosis."""

from dataclasses import dataclass, field
from typing import Dict, List, Any


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class DiagnosisResult:
    """Diagnostic symptoms and ordered parameter hypotheses.

    Responsibility fields are normalized heuristic priority weights, not measured
    causal contributions. Severity ranks suggestions; reasoning describes the
    available evidence, and improvement_trend compares supplied NSE records."""
    primary_issue: str = "none"
    severity: str = "minor"
    severity_score: float = 0.0
    vic_responsibility: float = 0.0
    routing_responsibility: float = 0.0
    upstream_responsibility: float = 0.0
    reasoning: str = ""
    recommendations: List[Dict[str, Any]] = field(default_factory=list)
    improvement_trend: str = "stagnant"
    diagnostic_status: str = "complete"
    unavailable_metrics: List[str] = field(default_factory=list)
    issue_status: Dict[str, str] = field(default_factory=dict)
    rule_conflicts: List[Dict[str, Any]] = field(default_factory=list)
    attribution_kind: str = "heuristic_prior_not_causal_estimate"
    rule_audit: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """Compact summary for Leader."""
        from .historical_policy import policy_worker_lines
        return " | ".join(policy_worker_lines(self, include_notice=False))


def diagnose_for_policy(mode, metrics, prev_metrics=None, context=None):
    """Dispatch the configured diagnostic policy without changing its rule engine."""
    from .diagnostic_evidence import validate_mode
    mode = validate_mode(mode)
    if mode == 'historical_policy':
        from .historical_policy import policy_diagnosis
        return policy_diagnosis(metrics, prev_metrics, context)
