"""Measured same-background acceptance and auditable joint accepted vectors."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import shutil
from pathlib import Path
from .experiment_contract import snapshot

PROTOCOL = "tra_dual_knowledge_joint_execution_v1"
class ExecutionIntegrityError(RuntimeError):
    pass

def context_identity(up_soil, up_routing):
    context = snapshot({"soil": up_soil or [], "routing": up_routing or []})
    digest = hashlib.sha256(json.dumps(context, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return context, digest

def finite_success(record):
    score=(record or {}).get("metrics", {}).get("NSE")
    return (record or {}).get("ok") is True and isinstance(score,(int,float)) and not isinstance(score,bool) and math.isfinite(score)

def vector_fingerprint(vector):
    return hashlib.sha256(json.dumps(vector,sort_keys=True,allow_nan=False).encode()).hexdigest()

def choose_confirmation(output_root, before_round, stations, fallback_centers=None):
    candidates=[]
    for rid in range(1,before_round):
        folder=Path(output_root)/f"round_{rid:03d}"
        try:
            receipt=json.loads((folder/"joint_execution_receipt.json").read_text())
            if receipt.get("status") != "consistent" or receipt.get("round_id") != rid:
                continue
            rows={sid:json.loads((folder/"results"/f"worker_{sid}.json").read_text())["result"] for sid in stations}
            if not all(finite_success(row) for row in rows.values()):
                continue
            vector={sid:row["params"] for sid,row in rows.items()}
            if vector_fingerprint(vector)!=receipt.get("vector_sha256"):
                raise ExecutionIntegrityError("Prior joint vector differs from its execution receipt")
            mean=math.fsum(rows[sid]["metrics"]["NSE"] for sid in sorted(stations))/len(stations)
            candidates.append((mean,-rid,vector))
        except (OSError,KeyError,TypeError,ValueError):
            continue
    if not candidates:
        raise ExecutionIntegrityError("No verified complete prior joint vector for terminal confirmation")
    mean,negative_rid,vector=max(candidates,key=lambda item:(item[0],item[1]))
    return {"protocol":PROTOCOL,"terminal_round":before_round,"source_round":-negative_rid,
            "source_online_mean_nse":mean,"selection_rule":"highest_mean_nse_consistent_accepted_joint_round_earliest_tie",
            "params_by_subbasin":copy.deepcopy(vector),"vector_sha256":vector_fingerprint(vector)}

def center_reference(state, worker, executor, center, up_soil, up_routing):
    sid,rid=worker.subbasin_id,state["round_id"]
    context,context_id=context_identity(up_soil,up_routing)
    downstream=bool(state.get("upstream_map",{}).get(sid))
    if rid>1 and downstream and not state.get("is_final_round"):
        routing_path=Path(state["round_dir"])/"center_references"/"routing_result"/sid
        lookup=getattr(executor,"lookup_center_reference",None)
        hit=lookup(sid,dict(center),up_soil,up_routing) if lookup else None
        if hit is not None:
            origin=hit["record"]
            execution={k:origin[k] for k in ("run_id","candidate_id","phase_tag")}
            source_round=(execution["run_id"]-execution["candidate_id"])//100
            executor.reuse_center_reference(hit,routing_path,rid)
            record={"round_id":source_round,"logical_round_id":rid,"params":dict(center),
                    "metrics":copy.deepcopy(origin["metrics"]),"ok":True,
                    "source":"reused_center_reference","execution_identity":execution,
                    "routing_result_relative":str(routing_path.relative_to(Path(state["output_root"]))),
                    "additional_model_evaluations":0,
                    "reference_reuse":{"protocol":"same-input-center-reference-v1",
                        "cache_key":hit["cache_key"],"source_attempt_id":origin["attempt_id"],
                        "source_execution_identity":execution,"source_round_id":source_round,
                        "logical_round_id":rid}}
        else:
            execution={"run_id":rid*100,"candidate_id":0,"phase_tag":f"center_reference_round_{rid}"}
            ok,metrics=executor.run(subbasin_id=sid,params=dict(center),upstream_soil_info=up_soil,
                                   upstream_routing_info=up_routing,**execution)
            cfg=getattr(executor,"_vic_configs",{}).get(sid)
            if ok and cfg:
                src=Path(cfg.base_output_dir)/f"{sid}_calibration_ai"/"current_run"/"routing_result"
                if src.is_dir():
                    shutil.copytree(src,routing_path,dirs_exist_ok=True)
            record={"round_id":rid,"logical_round_id":rid,"params":dict(center),"metrics":metrics,"ok":ok,
                    "source":"downstream_center_reference","execution_identity":execution,
                    "routing_result_relative":str(routing_path.relative_to(Path(state["output_root"]))),
                    "additional_model_evaluations":1}
    else:
        record=copy.deepcopy(state.get("accepted_center_records",{}).get(sid))
        if state.get("is_final_round"):
            return None
        if record and (record.get("params")!=center or record.get("upstream_context_id")!=context_id):
            raise ExecutionIntegrityError(f"{sid}: reused center evidence has a different parameter/background identity")
    if record is None:
        if rid>1:
            raise ExecutionIntegrityError(f"{sid}: missing accepted center evidence")
        return None
    if not finite_success(record):
        raise ExecutionIntegrityError(f"{sid}: failed/nonfinite center reference; acceptance cannot be compared")
    record.update(upstream_context=context,upstream_context_id=context_id,protocol=PROTOCOL)
    folder=Path(state["round_dir"])/"center_references"
    folder.mkdir(parents=True,exist_ok=True)
    (folder/f"{sid}.json").write_text(json.dumps(snapshot(record),indent=2,allow_nan=False)+"\n")
    return record

def candidate_record(result):
    return {"params":copy.deepcopy(result.get("params",result.get("best_params"))),
            "metrics":copy.deepcopy(result["metrics"]),"ok":result.get("ok"),
            "round_id":result.get("executed_round_id"),"upstream_context":result.get("upstream_context"),
            "upstream_context_id":result.get("upstream_context_id"),
            "execution_identity":result.get("execution_identity"),
            "routing_result_relative":result.get("routing_result_relative"),
            "source":"accepted_candidate","protocol":PROTOCOL}

def select_accepted(result, center, previous_record, final=False):
    reference=result.get("center_reference")
    valid=finite_success(result)
    if final:
        if not valid:
            raise ExecutionIntegrityError("Terminal confirmation candidate failed")
        return False,candidate_record(result),"frozen_joint_vector_confirmation"
    if reference is not None and (not finite_success(reference) or reference.get("params")!=center
            or reference.get("upstream_context_id")!=result.get("upstream_context_id")):
        raise ExecutionIntegrityError("Center reference does not match candidate background/current center")
    if reference is None and result.get("executed_round_id",1)>1:
        raise ExecutionIntegrityError("Missing same-background acceptance reference")
    accepted=valid and (reference is None or result["metrics"]["NSE"]>=reference["metrics"]["NSE"])
    chosen=candidate_record(result) if accepted else reference
    if not finite_success(chosen):
        raise ExecutionIntegrityError("No valid accepted center remains; joint score cannot be constructed")
    return accepted,copy.deepcopy(chosen),"candidate_nse_vs_same_background_center" if valid else "invalid_candidate_keep_verified_center"


def canonical_phase(phase):
    """Normalize supported enum/string spelling; unknown phases fail closed."""
    from .param_validator import CalibrationPhase
    value = getattr(phase, "value", phase)
    if not isinstance(value, str):
        raise ExecutionIntegrityError("Missing or invalid calibration phase")
    try:
        return CalibrationPhase(value.strip().upper())
    except ValueError as error:
        raise ExecutionIntegrityError(f"Unknown calibration phase: {value!r}") from error


def decision_phase_context(state):
    """Keep evidence identity separate from the phase of the next executable plan."""
    from .baseline_control import phase_progress
    from .phase_config import check_phase_transition
    observed = canonical_phase(state["current_phase_name"]).value
    rid = state["round_id"]
    terminal = state.get("is_final_round", False) or rid >= state["max_rounds"]
    target = observed
    target_round_in_phase = state["rounds_in_current_phase"] + 1
    if not terminal:
        progress, valid = phase_progress(state)
        advance, next_idx, _ = check_phase_transition(
            state["phase_pipeline"], state["current_phase_idx"],
            state["rounds_in_current_phase"],
            progress["rounds_without_improvement_in_phase"] if valid else 0,
        )
        if advance and next_idx is not None:
            target = canonical_phase(state["phase_pipeline"][next_idx]["name"]).value
            target_round_in_phase = 1
    return {
        "observed_round_id": rid, "observed_phase": observed,
        "decision_target_round_id": None if terminal else rid + 1,
        "decision_target_phase": target,
        "decision_target_round_in_phase": None if terminal else target_round_in_phase,
        "decision_purpose": "terminal_review_only" if terminal else "next_round_guidance",
    }


def bind_phase_decision(decision, context):
    """Authoritatively bind a reviewed round to its next plan, preserving raw actions."""
    from .param_validator import get_tunable_keys
    output = copy.deepcopy(decision)
    identity = {
        "source_round_id": context["observed_round_id"],
        "observed_phase": context["observed_phase"],
        "target_round_id": context["decision_target_round_id"],
        "target_phase": context["decision_target_phase"],
        "purpose": context["decision_purpose"],
    }
    output["_llm_round_id"] = output.get("round_id")
    output["round_id"] = identity["source_round_id"]
    output["_guidance_identity"] = identity
    active = set(get_tunable_keys(canonical_phase(identity["target_phase"])))
    if identity["purpose"] == "terminal_review_only":
        active = set()
    for guidance in output.get("worker_guidance", []):
        guidance["_guidance_identity"] = copy.deepcopy(identity)
        retained, excluded = [], []
        for action in guidance.get("param_adjustments", []):
            if action.get("param") in active:
                retained.append(action)
            else:
                excluded.append({"action": copy.deepcopy(action),
                                 "reason": "terminal_review_only" if not active else "locked_in_target_phase",
                                 "target_phase": identity["target_phase"]})
        guidance["param_adjustments"] = retained
        guidance["_phase_excluded_actions"] = excluded
    return output


def require_guidance_identity(state, decision, guidance, sid):
    """Reject stale/missing plans before center/model calls; frozen actions stay inactive."""
    from .param_validator import get_tunable_keys
    rid = state["round_id"]
    if rid == 1:
        if decision is not None:
            raise ExecutionIntegrityError("Initial default round cannot consume Leader guidance")
        return
    identity = (decision or {}).get("_guidance_identity", {})
    phase = canonical_phase(state["current_phase_name"]).value
    expected = {"source_round_id": rid - 1, "target_round_id": rid,
                "target_phase": phase, "purpose": "next_round_guidance"}
    if any(identity.get(k) != value for k, value in expected.items()):
        raise ExecutionIntegrityError(f"{sid}: stale/missing Leader guidance identity for round {rid}/{phase}")
    if not isinstance(guidance, dict) or guidance.get("subbasin_id") != sid:
        raise ExecutionIntegrityError(f"{sid}: missing or misbound station guidance")
    if guidance.get("_guidance_identity") != identity:
        raise ExecutionIntegrityError(f"{sid}: guidance identity differs from reviewed decision")
    active = set(get_tunable_keys(canonical_phase(phase)))
    if any(a.get("param") not in active for a in guidance.get("param_adjustments", [])):
        raise ExecutionIntegrityError(f"{sid}: frozen action found in effective guidance")
