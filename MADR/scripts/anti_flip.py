"""Scoped anti-flip bookkeeping from measured, selected local experiments.

Only a one-parameter, same-background NSE gain establishes direction evidence.
Deferrals are a bounded control timeout, never evidence that a direction failed.
No optimizer, model evaluation, prompt template or acceptance rule lives here.
"""
from copy import deepcopy

from .attribution_evidence import _record
from .execution_protocol import KEYS, is_finite, require_effective_params

PROTOCOL = "measured_direction_momentum_v1"
MIN_FAIL_TO_FLIP = 3
MAX_DEFERRALS = 3
SUPPORT_KEYS = ("evaluation_start", "evaluation_end", "n_scored_days")


def execution_scope(phase, upstream_context_id, reference, input_fingerprint):
    """The phase/background/scoring identity in which evidence may be reused."""
    phase = getattr(phase, "value", phase)
    if (phase not in ("VIC_ONLY", "ROUTING_ONLY", "JOINT")
            or not upstream_context_id or not input_fingerprint or not reference
            or reference.get("ok") is not True
            or not is_finite(reference.get("metrics", {}).get("NSE"))
            or reference.get("upstream_context_id") != upstream_context_id):
        return None
    metrics = reference.get("metrics", {})
    support = {k: metrics.get(k) for k in SUPPORT_KEYS}
    if (not isinstance(support["evaluation_start"], str)
            or not isinstance(support["evaluation_end"], str)
            or type(support["n_scored_days"]) is not int or support["n_scored_days"] <= 0):
        return None
    return {"phase": phase, "upstream_context_id": upstream_context_id,
            "input_fingerprint": input_fingerprint, "scoring_support": support}


def _count(entry, key):
    value = entry.get(key, 0)
    return value if type(value) is int and value >= 0 else 0


def apply_anti_flip(direction_history, sid, hints, scope):
    """Read-only filtering; the returned audit is consumed after this round."""
    station = direction_history.get(sid, {})
    active = bool(scope) and station.get("_scope") == scope
    audit = {"protocol": PROTOCOL, "scope": deepcopy(scope), "blocked": []}
    filtered = []
    for original in hints:
        hint = deepcopy(original)
        if not isinstance(hint, dict):
            filtered.append(hint)
            continue
        parameter = hint.get("parameter", hint.get("param", ""))
        direction = hint.get("direction", "hold")
        previous = station.get(parameter) if active else None
        if (parameter not in KEYS or direction not in ("increase", "decrease")
                or hint.get("magnitude", "none") == "none"
                or not isinstance(previous, dict)
                or previous.get("dir") not in ("increase", "decrease")
                or not previous.get("source_round_id")
                or direction == previous["dir"]
                or _count(previous, "fail_count") >= MIN_FAIL_TO_FLIP
                or _count(previous, "deferral_count") >= MAX_DEFERRALS):
            filtered.append(hint)
            continue
        if previous.get("last_outcome") == "gain":
            hint["direction"] = previous["dir"]
            hint["_antiflip"] = f"blocked: measured {previous['dir']} gain"
            action = "redirect"
        else:
            hint["direction"] = "hold"
            hint["magnitude"] = "none"
            hint["_antiflip"] = (f"hold: {_count(previous, 'fail_count')} measured no-gain trials, "
                                 f"{_count(previous, 'deferral_count')} deferrals")
            action = "hold"
        audit["blocked"].append({"parameter": parameter, "protected_dir": previous["dir"],
                                 "requested_dir": direction, "action": action})
        filtered.append(hint)
    return filtered, audit


def _observation(result, proposal, scope, round_id, sid):
    """Return one selected-candidate observation, without per-MC multiplication."""
    evidence = result.get("attribution_evidence") or {}
    if (not scope or proposal.get("ok") is not True
            or proposal.get("subbasin_id") != sid
            or proposal.get("executed_round_id") != round_id
            or proposal.get("executed_phase") != scope["phase"]
            or evidence.get("protocol") != "worker_background_decomposition_v1"
            or evidence.get("stage") != "completed_round"
            or evidence.get("status") != "ordinary"
            or evidence.get("round_id") != round_id or evidence.get("subbasin_id") != sid
            or evidence.get("phase") != scope["phase"]
            or evidence.get("scope", {}).get("input_fingerprint") != scope["input_fingerprint"]
            or evidence.get("comparisons", {}).get("proposed_local", {}).get("status") != "available"):
        return "unknown", None, None
    before = evidence.get("states", {}).get("B", {})
    candidate = evidence.get("states", {}).get("C", {})
    reference = proposal.get("center_reference") or {}
    params = proposal.get("params", proposal.get("best_params"))
    if (before.get("status") != "available" or candidate.get("status") != "available"
            or before != _record(reference) or candidate != _record(proposal)
            or before.get("upstream_context_id") != scope["upstream_context_id"]
            or candidate.get("upstream_context_id") != scope["upstream_context_id"]
            or before.get("scoring_support") != scope["scoring_support"]
            or candidate.get("scoring_support") != scope["scoring_support"]
            or before.get("execution_identity") != reference.get("execution_identity")
            or candidate.get("execution_identity") != proposal.get("execution_identity")
            or before.get("params") != reference.get("params") or candidate.get("params") != params):
        return "unknown", None, None
    base_params = before.get("params")
    try:
        require_effective_params(base_params)
        require_effective_params(params)
    except (TypeError, ValueError, KeyError):
        return "unknown", None, None
    base_nse = before.get("metrics", {}).get("NSE")
    candidate_nse = candidate.get("metrics", {}).get("NSE")
    if (not is_finite(base_nse) or not is_finite(candidate_nse)
            or base_nse != reference.get("metrics", {}).get("NSE")
            or candidate_nse != proposal.get("metrics", {}).get("NSE")):
        return "unknown", None, None
    changed = [p for p in KEYS if params[p] != base_params[p]]
    if not changed:
        return "no_op", None, None
    if len(changed) != 1:
        return "multiple_parameters", None, None
    parameter = changed[0]
    direction = "increase" if params[parameter] > base_params[parameter] else "decrease"
    return ("gain" if candidate_nse > base_nse else "no_gain"), parameter, direction


def update_direction_history(direction_history, sid, result, *, round_id, phase,
                             input_fingerprint, terminal=False):
    """Consume a completed station-round once; accepted ties are not gains.

Unknown/failed/no-op/multi-parameter candidates are not direction experiments.
If filtering deferred a reversal without a measured protected-direction trial,
count one deferral instead. Three deferrals release the restriction without
claiming three model failures. A real gain resets both counters.
"""
    updated = deepcopy(direction_history)
    if terminal:
        return updated
    existing = updated.get(sid, {})
    last_round = existing.get("_last_round")
    if type(last_round) is int and last_round >= round_id:
        return updated
    proposal = result.get("proposal_result") or result
    scope = execution_scope(phase, proposal.get("upstream_context_id"),
                            proposal.get("center_reference"), input_fingerprint)
    station = existing if scope and existing.get("_scope") == scope else {}
    station["_scope"] = deepcopy(scope)
    station["_last_round"] = round_id
    outcome, parameter, direction = _observation(result, proposal, scope, round_id, sid)
    station["_last_outcome"] = outcome
    for key in KEYS:
        if isinstance(station.get(key), dict):
            station[key]["last_outcome"] = "not_tested"
    measured = set()
    if parameter is not None:
        previous = station.get(parameter)
        if outcome == "gain":
            streak = (_count(previous, "streak") + 1
                      if isinstance(previous, dict) and previous.get("dir") == direction else 1)
            station[parameter] = {"dir": direction, "fail_count": 0, "deferral_count": 0,
                                  "streak": streak, "last_outcome": "gain",
                                  "source_round_id": round_id, "last_observation_round_id": round_id}
            measured.add(parameter)
        elif isinstance(previous, dict) and previous.get("dir") == direction:
            previous["fail_count"] = _count(previous, "fail_count") + 1
            previous["streak"] = _count(previous, "streak") + 1
            previous["last_outcome"] = "no_gain"
            previous["last_observation_round_id"] = round_id
            measured.add(parameter)
    audit = proposal.get("anti_flip_audit") or {}
    if audit.get("protocol") == PROTOCOL and scope and audit.get("scope") == scope:
        deferred = set()
        for blocked in audit.get("blocked", []):
            parameter = blocked.get("parameter")
            previous = station.get(parameter)
            if (parameter in measured or parameter in deferred or parameter not in KEYS
                    or not isinstance(previous, dict)
                    or blocked.get("protected_dir") != previous.get("dir")):
                continue
            previous["deferral_count"] = _count(previous, "deferral_count") + 1
            previous["last_deferred_round_id"] = round_id
            deferred.add(parameter)
    updated[sid] = station
    return updated
