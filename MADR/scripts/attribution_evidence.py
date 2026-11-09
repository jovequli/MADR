"""Observed background/local contrasts; read existing records, never evaluate.

The archive is common to both methods and E modes. Only the MADR Worker caller
may render this view. Source identity and scoring support gate every subtraction.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .diagnostic_evidence import METRIC_KEYS
from .execution_protocol import KEYS, is_finite

PROTOCOL = "worker_background_decomposition_v1"
DELTA_PAIRS = (("background", "A", "B"), ("proposed_local", "B", "C"),
               ("accepted_local", "B", "D"), ("accepted_total", "A", "D"))


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not is_finite(value):
        return None
    return value


def _context_valid(record):
    context = record.get("upstream_context")
    identity = record.get("upstream_context_id")
    if not isinstance(context, dict) or set(context) != {"soil", "routing"} or not identity:
        return False
    try:
        return hashlib.sha256(json.dumps(context, sort_keys=True, allow_nan=False).encode()).hexdigest() == identity
    except (TypeError, ValueError):
        return False


def _record(record):
    record = record or {}
    metrics = record.get("metrics") or {}
    identity = record.get("execution_identity") or {}
    params = record.get("params", record.get("best_params"))
    evaluation_round = record.get("round_id", record.get("executed_round_id"))
    valid_identity = (type(evaluation_round) is int and evaluation_round > 0
                      and type(identity.get("run_id")) is int
                      and type(identity.get("candidate_id")) is int and identity["candidate_id"] >= 0
                      and identity["run_id"] == evaluation_round * 100 + identity["candidate_id"])
    if valid_identity:
        tag = identity.get("phase_tag")
        valid_identity = (tag == f"center_reference_round_{evaluation_round}" if identity["candidate_id"] == 0
                          else isinstance(tag, str) and tag.startswith(f"round_{evaluation_round}_"))
    valid_params = (isinstance(params, dict) and set(params) == set(KEYS)
                    and all(is_finite(params[k]) for k in KEYS))
    status = ("missing_record" if not record else "failed_evaluation" if record.get("ok") is not True
              else "nonfinite_nse" if not is_finite(metrics.get("NSE"))
              else "missing_execution_identity" if not valid_identity
              else "invalid_parameter_identity" if not valid_params
              else "invalid_upstream_identity" if not _context_valid(record) else "available")
    return _clean({
        "status": status, "source": record.get("source", record.get("accepted_record_source")),
        "evaluation_round_id": evaluation_round,
        "execution_identity": identity, "params": params,
        "upstream_context_id": record.get("upstream_context_id"),
        "upstream_context": record.get("upstream_context"),
        "routing_result_relative": record.get("routing_result_relative"),
        "metrics": {k: metrics.get(k) if record.get("ok") is True and is_finite(metrics.get(k)) else None
                    for k in METRIC_KEYS},
        "scoring_support": {k: metrics.get(k) for k in
                            ("evaluation_start", "evaluation_end", "n_scored_days")},
        "diagnostic_status": metrics.get("diagnostic_status"),
    })


def _difference(before, after, *, scope, extra_reason=None):
    reason = extra_reason
    if not reason:
        reason = next((r["status"] for r in (before, after) if r["status"] != "available"), None)
    if not reason and not scope.get("input_fingerprint"):
        reason = "unverified_run_input_scope"
    if not reason:
        support = before["scoring_support"]
        if (not isinstance(support.get("evaluation_start"), str)
                or not isinstance(support.get("evaluation_end"), str)
                or type(support.get("n_scored_days")) is not int or support["n_scored_days"] <= 0):
            reason = "missing_scoring_support"
        elif support != after["scoring_support"]:
            reason = "different_scoring_support"
    values = {k: (after["metrics"][k] - before["metrics"][k]
                  if not reason and is_finite(before["metrics"][k]) and is_finite(after["metrics"][k]) else None)
              for k in METRIC_KEYS}
    return {"status": reason or "available", "delta": values,
            "unavailable_metrics": [k for k, v in values.items() if v is None]}


def before_proposal(*, round_id, sid, phase, previous, reference, input_fingerprint=None,
                    mode=None, terminal=False):
    """A/B only. This function cannot accept C/D or candidate metrics."""
    states = {"A": _record(previous), "B": _record(reference)}
    stage_status = "terminal_confirmation" if terminal else "initialization" if round_id == 1 else "ordinary"
    scope = {"input_fingerprint": input_fingerprint, "execution_mode": mode}
    reason = stage_status if stage_status != "ordinary" else (
        "different_local_parameters" if states["A"]["params"] != states["B"]["params"] else None)
    background = _difference(states["A"], states["B"], scope=scope, extra_reason=reason)
    return {"protocol": PROTOCOL, "stage": "before_proposal", "round_id": round_id,
            "subbasin_id": sid, "phase": phase, "status": stage_status,
            "evidence_ref": f"R{round_id}:{sid}:attribution", "scope": scope,
            "states": states, "comparisons": {"background": background}}


def completed_round(before, *, candidate, accepted_record, accepted, reason):
    """Build the selected-candidate/accepted-state archive after real selection."""
    result = deepcopy(before)
    result["stage"] = "completed_round"
    result["accepted_as_center"] = bool(accepted)
    result["acceptance_reason"] = reason
    result["states"].update(C=_record(candidate), D=_record(accepted_record))
    states, scope = result["states"], result["scope"]
    special = result["status"] if result["status"] != "ordinary" else None
    selected = states["C"] if accepted else states["B"]
    selection_matches = all(selected[key] == states["D"][key] for key in
                            ("params", "metrics", "execution_identity", "upstream_context_id"))
    result["acceptance_consistency"] = ("not_search_selection" if special else
                                        "verified" if selection_matches else "accepted_state_mismatch")
    for name, left, right in DELTA_PAIRS[1:]:
        pair_reason = special
        if not pair_reason and name in ("accepted_local", "accepted_total") and not selection_matches:
            pair_reason = "accepted_state_mismatch"
        if not pair_reason and name in ("proposed_local", "accepted_local"):
            if states[left]["upstream_context_id"] != states[right]["upstream_context_id"]:
                pair_reason = "different_upstream_background"
        if not pair_reason and name == "accepted_total":
            # Only name this the total of the stated path if its A/B link is sound.
            status = result["comparisons"]["background"]["status"]
            pair_reason = status if status != "available" else None
            if not pair_reason and states["B"]["upstream_context_id"] != states["D"]["upstream_context_id"]:
                pair_reason = "different_upstream_background"
        result["comparisons"][name] = _difference(states[left], states[right], scope=scope,
                                                   extra_reason=pair_reason)
    residual = {}
    for key in METRIC_KEYS:
        values = [result["comparisons"][name]["delta"][key]
                  for name in ("background", "accepted_local", "accepted_total")]
        residual[key] = values[2] - values[0] - values[1] if all(is_finite(v) for v in values) else None
    result["additive_residual"] = residual
    return _clean(result)


def write_evidence(round_dir, sid, stage, evidence):
    # Use the existing critical trace writer; do not alter physical selection.
    from .process_trace import write_json
    return write_json(Path(round_dir) / "attribution" / f"{sid}_{stage}.json", evidence)
