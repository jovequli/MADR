"""Code Worker proposal strategy for the shared MADR/SADR execution graph.

The Leader, diagnostics, candidate evaluation, acceptance and evidence handling
remain shared. This module only supplies SADR proposals and escape state.
"""
from __future__ import annotations
import copy
import random
from .worker_state import MAGNITUDE_FRACTION
from .param_validator import get_locked_keys, normalize_params

MC_STEP_SCALES = ((0.3, "conservative"), (0.7, "moderate"), (1.0, "normal"))


def initial_escape_states(workers):
    return {worker["subbasin_id"]: {"in_escape":False, "tier":0,
        "agent_stagnation":0, "round_in_tier":0,
        "found_improvement_in_tier":False} for worker in workers}


def phase_decay(phase, local_round):
    floors={"VIC_ONLY":0.05,"ROUTING_ONLY":0.30,"JOINT":0.10}
    starts={"VIC_ONLY":1.00,"ROUTING_ONLY":0.40,"JOINT":0.15}
    if phase not in floors or type(local_round) is not int or local_round<1:
        raise ValueError("Invalid SADR phase or local round")
    return max(floors[phase], starts[phase]*0.95**local_round)


def escape_parameters(esc: dict) -> dict:
    """Return escape perturbation config based on current tier."""
    tier = esc.get("tier", 1)
    n_active = 6  # The executor caps the perturbation set to the active phase keys.
    if tier == 1:
        return {"tier": 1, "perturbation_pct": 0.10,
                "n_params_to_perturb": max(1, n_active // 2)}
    elif tier == 2:
        return {"tier": 2, "perturbation_pct": 0.15,
                "n_params_to_perturb": max(1, n_active // 2 + 1)}
    else:
        round_in_tier = esc.get("round_in_tier", 0)
        return {"tier": 3, "perturbation_pct": 0.20 + 0.01 * round_in_tier,
                "n_params_to_perturb": 0}  # 0 = all params


def _advance_escape(esc: dict, improved: bool) -> str:
    """Advance one valid search round using its measured local improvement."""
    STAGNATION_TRIGGER = 10
    TIER_ROUNDS = 5

    if not esc.get("in_escape", False):
        if improved:
            esc["agent_stagnation"] = 0
        else:
            esc["agent_stagnation"] = esc.get("agent_stagnation", 0) + 1
            if esc["agent_stagnation"] >= STAGNATION_TRIGGER:
                esc["in_escape"] = True
                esc["tier"] = 1
                esc["round_in_tier"] = 0
                esc["found_improvement_in_tier"] = False
                return "ENTER_ESCAPE"
    else:
        if improved:
            esc["found_improvement_in_tier"] = True
        esc["round_in_tier"] = esc.get("round_in_tier", 0) + 1

        tier = esc.get("tier", 1)
        if tier <= 2 and esc["round_in_tier"] >= TIER_ROUNDS:
            if esc.get("found_improvement_in_tier", False):
                esc["in_escape"] = False
                esc["tier"] = 0
                esc["agent_stagnation"] = 0
                return f"EXIT_ESCAPE T{tier} improved"
            else:
                esc["tier"] = tier + 1
                esc["round_in_tier"] = 0
                esc["found_improvement_in_tier"] = False
                return f"TIER_UP {tier}→{tier + 1}"
        elif tier >= 3:
            if improved:
                esc["in_escape"] = False
                esc["tier"] = 0
                esc["agent_stagnation"] = 0
                return "EXIT_ESCAPE T3 local improvement"
    return ""


def update_escape_state(esc: dict, *, candidate: dict, reference: dict | None,
                        current_center: dict, accepted: bool, round_id: int,
                        phase: str, terminal: bool = False) -> dict:
    """Use the existing same-background evaluation; never consult the global ledger.

    Default and terminal confirmation rounds are not search opportunities.
    Failed candidates do not establish improvement or advance escape counters.
    The receipt retains the measured pair separately from acceptance: a tie can
    be accepted without improving by the escape threshold.
    """
    from .execution_consistency import ExecutionIntegrityError, finite_success
    from .native_audit import json_safe

    def measured(record):
        record = record or {}
        return {key: copy.deepcopy(record.get(key)) for key in (
            "round_id", "executed_round_id", "logical_round_id", "params", "metrics",
            "ok", "execution_identity", "upstream_context_id", "source")}

    receipt = {"protocol":"sadr_same_background_escape_v1", "round_id":round_id,
        "phase":phase, "subbasin_id":candidate.get("subbasin_id"),
        "criterion":"candidate_NSE > same_background_reference_NSE + 0.001",
        "threshold":0.001, "reference":measured(reference),
        "candidate":measured(candidate), "accepted":accepted,
        "upstream_context_id":candidate.get("upstream_context_id"),
        "delta_NSE":None, "local_improved":False,
        "state_before":copy.deepcopy(esc), "event":""}
    if round_id == 1:
        receipt["status"] = "skipped_default_round"
    elif terminal:
        receipt["status"] = "skipped_terminal_confirmation"
    else:
        if (not finite_success(reference) or reference.get("params") != current_center
                or not reference.get("upstream_context_id")
                or (candidate.get("upstream_context_id") is not None
                    and candidate.get("upstream_context_id") != reference["upstream_context_id"])):
            raise ExecutionIntegrityError("SADR escape requires the measured same-background center")
        if not finite_success(candidate):
            receipt["status"] = "skipped_invalid_candidate"
        else:
            if candidate.get("upstream_context_id") != reference["upstream_context_id"]:
                raise ExecutionIntegrityError("SADR escape candidate has no matching upstream identity")
            nse, baseline = candidate["metrics"]["NSE"], reference["metrics"]["NSE"]
            receipt["delta_NSE"] = nse - baseline
            # Comparing the two levels avoids turning an exact threshold tie
            # into improvement through subtraction round-off.
            improved = nse > baseline + receipt["threshold"]
            receipt.update(status="evaluated", local_improved=improved,
                           event=_advance_escape(esc, improved))
    receipt["state_after"] = copy.deepcopy(esc)
    return json_safe(receipt)


class RuleWorkerStrategy:
    def configure(self, *, phase, local_round, escape_state, guidance):
        self.decay=phase_decay(phase,local_round)
        self.escape=escape_parameters(escape_state) if escape_state.get("in_escape") else None
        self.guidance=copy.deepcopy(guidance or {"param_adjustments":[]})

    def _base(self, worker, center):
        reference=getattr(worker,"current_reference_record",None)
        if reference and reference.get("params")!=center:
            raise ValueError("SADR center differs from the measured same-background reference")
        return dict(center)

    def _trace(self, branch, center, **details):
        return {"branch":branch,"source":"escape_rule" if self.escape else "rule_worker",
                "call_id":None,"reasoning":"Code-executed Leader plan" if not self.escape else "Phase-local escape proposal",
                "leader_instruction_received":copy.deepcopy(self.guidance),"base_params":dict(center),
                "decay_factor":self.decay,"escape_info":copy.deepcopy(self.escape),
                "fallback_reason":None,"slot_recovery":None,**details}

    def single(self,worker,center,phase,radius):
        base=self._base(worker,center)
        if self.escape:
            generated=worker.executor.generate_escape_candidates(base_params=base,phase=phase.lower(),
                perturbation_pct=self.escape["perturbation_pct"],n_params_to_perturb=self.escape["n_params_to_perturb"])
            trace=self._trace("escape_rule",base)
        elif not self.guidance.get("param_adjustments"):
            generated=[dict(base)]
            trace=self._trace("execute_instruction",base,reasoning="Leader-directed hold: exact current center without perturbation.")
        else:
            generated=worker.executor.generate_candidates(base_params=base,leader_instruction=self.guidance,
                phase=phase.lower(),radius=radius,decay_factor=self.decay)
            trace=self._trace("execute_instruction",base,**(getattr(worker.executor,"last_instruction_trace",None) or {}))
        if not generated:
            raise ValueError("SADR rule strategy did not supply its candidate")
        trace["normalized_candidates"]=copy.deepcopy(generated)
        return generated,trace

    def multi(self,worker,center,phase,n_candidates):
        if type(n_candidates) is not int or not 1<=n_candidates<=len(MC_STEP_SCALES):
            raise ValueError("SADR MC requires an integer candidate count from 1 to 3")
        base=self._base(worker,center);active=worker.executor._get_active_keys(phase.lower())
        instructions={}
        for action in self.guidance.get("param_adjustments",[]):
            param=action.get("param",action.get("parameter",action.get("parameter_name",action.get("param_name",action.get("name","")))))
            direction=action.get("direction","hold");magnitude=action.get("magnitude","none")
            if param not in active or direction=="hold" or magnitude in ("none","0",0):continue
            instructions[param]=(1. if direction=="increase" else -1.,MAGNITUDE_FRACTION.get(str(magnitude).lower(),.1))
        results=[]
        for index,(scale,label) in enumerate(MC_STEP_SCALES[:n_candidates]):
            if self.escape:
                generated=worker.executor.generate_escape_candidates(base_params=base,phase=phase.lower(),
                    perturbation_pct=self.escape["perturbation_pct"],n_params_to_perturb=self.escape["n_params_to_perturb"])
                if len(generated)!=1:raise ValueError("Escape must supply exactly one candidate per MC slot")
                candidate=dict(generated[0]);label=f"escape_{index+1}"
                trace=self._trace("escape_multi_candidate",base,generated_escape_candidate=copy.deepcopy(candidate))
            else:
                candidate=dict(base);raw=dict(base);noise_values={}
                for key in active:
                    if key not in base:continue
                    lo,hi=worker.ranges.get(key,(0,1));span=hi-lo
                    if key in instructions:
                        sign,fraction=instructions[key]
                        noise=random.gauss(0,span*.01)
                        value=base[key]+sign*span*fraction*self.decay*scale+noise
                    else:
                        noise=random.gauss(0,span*.005);value=base[key]+noise
                    raw[key]=value;noise_values[key]=noise;candidate[key]=max(lo,min(hi,value))
                trace=self._trace("rule_multi_candidate",base,effective_instructions=instructions,
                    scale_factor=scale,raw_rule_proposal=raw,sampled_noise=noise_values)
            trace["candidate_before_locks"]=copy.deepcopy(candidate)
            for key in get_locked_keys(phase):candidate[key]=center[key]
            candidate=normalize_params(candidate,worker.ranges)
            trace["normalized_candidates"]=[dict(candidate)]
            results.append((candidate,label,trace))
        return results
