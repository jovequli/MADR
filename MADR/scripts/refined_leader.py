"""Opt-in Leader delivery projection; never a numerical or acceptance policy.

Inputs are the complete B-style blocks already constructed from runtime facts.
All source objects, including trial audit statuses, remain in the normal archive.
This view removes exactly repeated bookkeeping and keeps novel/unknown fields.
It never summarizes model reasoning, rounds a number or merges executions.
"""
from __future__ import annotations

import copy
import json
import math
import re


MC_STRATEGIES = ("conservative", "moderate", "bold")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _metric_record(body, evaluation_ref):
    evaluation = body.get("evaluations", {}).get(evaluation_ref, {})
    return body.get("measurements", {}).get(evaluation.get("metrics"), {})


def _redundant_delta(delta, feedback, body):
    """Drop only differences exactly reproducible from the retained two M rows."""
    if not isinstance(delta, dict):
        return False
    after = _metric_record(body, feedback.get("selected_candidate"))
    before = _metric_record(body, feedback.get("comparison_anchor"))
    return all(_finite(before.get(key)) and _finite(after.get(key))
               and _finite(value) and after[key] - before[key] == value
               for key, value in delta.items())


def _trial_with_checks(trial, checks):
    """Join each original expectation to its actual check, without rejudging it.

    Duplicate expectations are paired by occurrence. Unmatched checks remain
    explicit. Past aliases stay qualified as supplied by compact_feedback.
    """
    if not isinstance(trial, dict):
        return copy.deepcopy(trial), copy.deepcopy(checks)
    shown = copy.deepcopy(trial)
    if (not isinstance(shown.get("expected_response", []), list)
            or not isinstance(checks or [], list)):
        return shown, copy.deepcopy(checks)
    remaining = list(enumerate(copy.deepcopy(checks or [])))
    responses = []
    for expectation in shown.get("expected_response", []):
        if not isinstance(expectation, dict):
            responses.append(expectation)
            continue
        item = copy.deepcopy(expectation)
        match = next(((index, check) for index, check in remaining
                      if isinstance(check, dict)
                      and check.get("metric") == item.get("metric")
                      and check.get("expected_direction") == item.get("direction")), None)
        if match:
            index, check = match
            remaining = [(i, c) for i, c in remaining if i != index]
            # The repetitive success phrase has a single legend; unavailable,
            # failed, unspecified and unexpected statuses remain literal.
            result = {k: v for k, v in check.items()
                      if k not in ("metric", "expected_direction")}
            if result.get("status") == "observed_numeric_comparison_only":
                result.pop("status")
            item["observed_check"] = result
        responses.append(item)
    if "expected_response" in shown:
        shown["expected_response"] = responses
    return shown, [check for _, check in remaining]


def _controls(changes):
    """Keep original scientific rationale distinct from a code-forced action."""
    result = []
    for change in changes:
        if not isinstance(change, dict):
            result.append(copy.deepcopy(change))
            continue
        row = copy.deepcopy(change)
        original = row.get("original")
        effective_key = "processed" if "processed" in row else "effective"
        effective = row.get(effective_key)
        if not isinstance(original, dict) or not isinstance(effective, dict):
            result.append(row)
            continue
        prior = copy.deepcopy(original)
        actual = copy.deepcopy(effective)
        original_reason = prior.pop("reasoning", None)
        effective_reason = actual.pop("reasoning", None)
        if original_reason is None:
            original_reason = prior.pop("reason", None)
        if effective_reason is None:
            effective_reason = actual.pop("reason", None)
        # Retain any distinct effective rationale. Identical inherited words
        # must not masquerade as an explanation of the code-changed direction.
        row.pop("original")
        row.pop(effective_key)
        row["original_LLM_action"] = prior
        row["effective_code_action"] = actual
        if original_reason is not None:
            row["original_LLM_reason"] = original_reason
        if effective_reason != original_reason:
            row["effective_reason_text"] = effective_reason
        if "reason" in row:
            row["code_control_reason"] = row.pop("reason")
        # Exact unchanged action metadata is shared once; changed values stay
        # in both actions. In particular, evidence refs are not rebound.
        shared = {key: prior[key] for key in list(prior)
                  if key not in ("direction", "magnitude")
                  and key in actual and actual[key] == prior[key]}
        for key in shared:
            prior.pop(key)
            actual.pop(key)
        if shared:
            row["shared_action_fields"] = shared
        result.append(row)
    return result


def _single_candidate_is_redundant(candidates, feedback, body):
    if not isinstance(candidates, list) or len(candidates) != 1:
        return False
    c = candidates[0]
    if not isinstance(c, dict) or set(c) != {"candidate_id", "NSE", "selected", "simulation_ok"}:
        return False
    evaluation = body.get("evaluations", {}).get(feedback.get("selected_candidate"), {})
    measured = _metric_record(body, feedback.get("selected_candidate"))
    return (c["candidate_id"] == evaluation.get("candidate_id")
            and c["selected"] is True
            and c["simulation_ok"] == evaluation.get("evaluation_ok")
            and "NSE" in measured and c["NSE"] == measured["NSE"])


def _feedback(feedback, sid, station, state, body):
    if not isinstance(feedback, dict):
        return copy.deepcopy(feedback), None
    result = copy.deepcopy(feedback)
    selected = body.get("evaluations", {}).get(result.get("selected_candidate"), {})
    # Identity remains in E; remove only provably identical role decoration.
    for key, expected in (("candidate_id", selected.get("candidate_id")),
                          ("round_id", selected.get("round_id")),
                          ("phase", selected.get("phase")),
                          ("subbasin_id", sid),
                          ("accepted_center_after_round", station.get("accepted_center"))):
        if key in result and result[key] == expected:
            result.pop(key)
    for key, routine in (
            ("trial_status", {"provided", "not_provided"}),
            ("trial_validation_status", {"valid", "missing"}),
            ("trial_evidence_status", {"provided_source_labels_only", "no_references"}),
            ("metric_evidence_status", {"available"})):
        if isinstance(result.get(key), str) and result[key] in routine:
            result.pop(key)
    trial = result.pop("trial", None)
    checks = result.pop("expectation_checks", [])
    if trial is not None:
        trial, remaining = _trial_with_checks(trial, checks)
        result["prior_proposal_trial"] = trial
        if remaining:
            result["unmatched_expectation_checks"] = remaining
    elif checks:
        result["expectation_checks_without_trial"] = checks
    else:
        # No hypothesis is manufactured for absent/unprovided annotations.
        if result.get("trial_source") is None:
            result.pop("trial_source", None)
    result.pop("trial_reference_scope", None)  # shared prior-ref legend below
    delta = result.get("metric_delta_from_anchor")
    if _redundant_delta(delta, feedback, body):
        result.pop("metric_delta_from_anchor", None)
    if "control_changes" in result:
        result["control_changes"] = _controls(result["control_changes"])
    candidates = result.pop("mc_candidates", None)
    if _single_candidate_is_redundant(candidates, feedback, body):
        candidates = None
    return result, candidates


def _mc_strategy_summary(raw, candidates_by_station):
    """Keep the strategy result, list exact candidate scores only once.

    We do not associate strategy and candidate by equality of rounded scores.
    Only the active fixed slot convention identifies c1..c5. An unrecognized
    summary is retained verbatim instead of silently throwing it away.
    """
    if not raw:
        return ""
    lines = raw.splitlines()
    projected = []
    numeric = r"(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|nan|inf|-inf)"
    for line in lines:
        match = re.fullmatch(r"([^:]+): best=([a-z_]+), (.+)", line.strip())
        if not match or match[1] not in candidates_by_station:
            return raw
        sid, best, responses = match.groups()
        parts = responses.split(", ")
        strategies = []
        candidate_by_id = {c.get("candidate_id"): c for c in candidates_by_station[sid]
                           if isinstance(c, dict)}
        for part in parts:
            pair = re.fullmatch(r"([a-z_]+)→(" + numeric + r")", part)
            if not pair or pair[1] not in MC_STRATEGIES:
                return raw
            slot = MC_STRATEGIES.index(pair[1]) + 1
            matching = [c for cid, c in candidate_by_id.items()
                        if isinstance(cid, str) and cid.endswith(f":{sid}:c{slot}")]
            # Verify this is really the standard rounded rendering of the
            # exact score in the retained table. Never match by score alone.
            if len(matching) != 1 or not _finite(matching[0].get("NSE")):
                return raw
            if format(matching[0]["NSE"], ".3f") != pair[2]:
                return raw
            strategies.append(pair[1])
        if best not in strategies:
            return raw
        # Scores now exist in the exact MC list; original order is retained.
        projected.append(f"{sid}: best={best}; strategy_order=" + ",".join(strategies))
    return "\n".join(projected)


def refine_leader_blocks(agent, blocks, records) -> dict[str, str]:
    """Return a non-mutating model-facing E projection of existing B blocks."""
    from .refined_common import refine_records

    result = copy.deepcopy(blocks)
    state = json.loads(result["state"])
    body = json.loads(result["records"])
    candidates = {}
    common_scopes = {}
    stations = state.get("stations", {})
    # Deduplicate only exactly equal scope strings shared across all stations.
    for field, nested in (("round_summary", "nse_delta_scope"),
                          ("stagnation_scope", "criterion")):
        rows = [row.get(field) for row in stations.values()]
        if rows and all(isinstance(row, dict) and nested in row for row in rows):
            values = [row[nested] for row in rows]
            if all(value == values[0] for value in values):
                common_scopes[field + "." + nested] = values[0]
                for row in rows:
                    row.pop(nested)
    for sid, station in stations.items():
        if "selected_candidate_feedback" not in station:
            continue
        compact, scores = _feedback(station["selected_candidate_feedback"], sid,
                                    station, state, body)
        station["selected_candidate_feedback"] = compact
        if scores is not None:
            candidates[sid] = scores
    if common_scopes:
        state["shared_station_field_meanings"] = common_scopes
    result["state"] = _json(state)
    result["records"] = _json(refine_records(body))
    # Legend text comes from the Markdown prompt source, never hard-coded
    # professional knowledge. Parent hook also uses it for the Worker role.
    result["record_legend"] = agent.prompt_source.get_text("refined_record_legend")
    result["feedback_legend"] = agent.prompt_source.get_text("refined_leader_feedback_legend")
    if candidates:
        result["candidate_results"] = _json(candidates)
    raw_mc = result.get("MC_response", "")
    result["MC_response"] = _mc_strategy_summary(raw_mc, candidates)
    if raw_mc and result["MC_response"] != raw_mc:
        result["MC_response"] += "\n" + agent.prompt_source.get_text("refined_mc_slots")
    return result
