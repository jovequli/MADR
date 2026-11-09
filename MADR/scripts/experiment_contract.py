"""Small, additive provenance and feedback contract; never changes optimization math."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import is_dataclass, asdict
import json
import math
from .prompt_source import get_text


def evidence_index(metrics, feedbacks=(), sources=()):
    refs = set(sources)
    refs.update("metric:" + str(k) for k in (metrics or {}))
    for f in feedbacks:
        if not f:
            continue
        refs.add(f["candidate_id"])
        refs.update(c["candidate_id"] for c in f.get("mc_candidates", []))
    return sorted(refs)


def reference_audit(refs, provided):
    available = set(provided or [])
    return [{"reference": r, "status": "provided_source_label" if r in available else "unverified_reference"}
            for r in (refs or [])]


from pathlib import Path

EXPERIMENT_CONTRACT_PROTOCOL = 'tra_dual_knowledge_optional_trial_v3'
from .output_contract import load_output_schema, validate_schema

TRIAL_SCHEMA = load_output_schema('worker')['properties']['trial']['x-annotation-schema']


def snapshot(value):
    """JSON-safe metadata only; preserves all actions and does not mutate input."""
    if is_dataclass(value) and not isinstance(value, type):
        return snapshot(asdict(value))
    if isinstance(value, dict):
        return {str(k): snapshot(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def validate_trial(trial):
    """The annotation shape has the same source as the visible output contract."""
    if trial is not None:
        validate_schema(trial, TRIAL_SCHEMA, '$.trial')


def audit_trial(payload, available_refs=None):
    """Preserve optional annotations without inventing evidence or hypotheses.

    A supplied hypothesis/uncertainty may omit the two annotation lists: retain
    its text with empty lists and mark it partial. This never changes actions.
    Malformed annotations remain archived and never invalidate legal parameters.
    """
    payload = payload if isinstance(payload, dict) else {}
    present = 'trial' in payload
    raw = payload.get('trial')
    errors = []; empty_lists = []; trial = None
    if not present:
        status = 'missing'
    elif raw is None:
        status = 'null'
    else:
        candidate = copy.deepcopy(raw)
        if isinstance(candidate, dict):
            for key in ('evidence_refs', 'expected_response'):
                if key not in candidate:
                    candidate[key] = []
                    empty_lists.append(key)
        try:
            validate_trial(candidate)
        except (ValueError, TypeError) as error:
            status = 'invalid'
            errors = [str(error)]
        else:
            status = 'partial' if empty_lists else 'valid'
            trial = candidate
    refs = reference_audit(trial['evidence_refs'] if trial is not None else [], available_refs)
    return {
        'trial_status': ('provided_partial' if status == 'partial' else
                         'provided' if status == 'valid' else
                         'invalid' if status == 'invalid' else 'not_provided'),
        'trial_validation_status': status,
        'trial_field_present': present,
        'trial': trial,
        'trial_raw': snapshot(raw),
        'trial_validation_errors': errors,
        'trial_empty_annotation_lists_added': empty_lists if trial is not None else [],
        'trial_reference_audit': refs,
        'trial_evidence_status': ('unverified_references' if any(
            r['status'] == 'unverified_reference' for r in refs) else
            'provided_source_labels_only' if refs else 'no_references'),
    }


def normalize_guidance(guidance):
    if not guidance:
        return guidance
    output = copy.deepcopy(guidance)
    output.setdefault('_leader_original', snapshot(guidance))
    audit = audit_trial(output['_leader_original'], output.get('_provided_evidence_refs', []))
    output['_trial_annotation'] = audit
    output['trial'] = copy.deepcopy(audit['trial'])
    adjustments = []
    for action in output.get('param_adjustments', []):
        item = dict(action)
        item['reasoning'] = item.get('reasoning') or item.get('reason', '')
        item.setdefault('evidence_refs', [])
        adjustments.append(item)
    output['param_adjustments'] = adjustments
    output['search_radius_hint'] = 'keep'
    output['_radius_policy'] = 'inactive; radius is not consumed by candidate generation'
    return output


def normalize_decision(decision, available_refs=None):
    output = copy.deepcopy(decision)
    if 'trial' in output:
        # A run-wide explanation has no reliable station/candidate binding. Keep
        # the original value and audit it, but never distribute it to workers.
        output['_unassigned_trial_annotation'] = {
            **audit_trial(output, available_refs),
            'association_status': 'unassigned_top_level',
            'eligible_for_candidate_feedback': False,
            'binding_note': 'Top-level trial retained only as an unassigned explanation; '
                            'not copied into worker_guidance or candidate feedback.',
        }
    output['worker_guidance'] = [normalize_guidance(g) for g in output.get('worker_guidance', [])]
    for g in output['worker_guidance']:
        g['_provided_evidence_refs'] = list(available_refs or [])
        g['_trial_annotation'] = audit_trial(g['_leader_original'], available_refs)
        g['_trial_reference_audit'] = g['_trial_annotation']['trial_reference_audit']
        for action in g.get('param_adjustments', []):
            action['_evidence_ref_audit'] = reference_audit(action.get('evidence_refs'), available_refs)
    return output


def bind_processed_guidance(guidance, filtered):
    output = normalize_guidance(guidance)
    if not output:
        return output
    before = copy.deepcopy(output.get('param_adjustments', []))
    output['param_adjustments'] = copy.deepcopy(filtered)
    output['_anti_flip_changes'] = [
        {'param': after.get('param'), 'original': original, 'processed': after,
         'reason': after.get('_antiflip', 'legacy anti-flip control')}
        for original, after in zip(before, filtered)
        if (original.get('direction'), original.get('magnitude')) !=
           (after.get('direction'), after.get('magnitude'))
    ]
    return output


def render_worker_plan(guidance, filtered=None, phase=None, static_input_protocol=None, context_delivery_protocol=None):
    """Full plan, reasons and references; no [:4] or other action truncation."""
    if not guidance:
        return ''
    processed = bind_processed_guidance(guidance, filtered) if filtered is not None else normalize_guidance(guidance)
    from .context_delivery import compact_worker_plan
    return compact_worker_plan(processed, phase)


def _parameter_payload(proposal, before_locks):
    parsed = (proposal or {}).get('raw_parsed_proposal') or {}
    if isinstance(parsed, dict) and ('vic' in parsed or 'routing' in parsed):
        return {**parsed.get('vic', {}), **parsed.get('routing', {})}
    return snapshot((proposal or {}).get('raw_rule_proposal') or before_locks)


def candidate_record(*, round_id, sid, candidate_number, phase, branch, guidance,
                     proposal, before_locks, effective_params, metrics, ok,
                     trial_source, reasoning='', worker_context='', base_record=None,
                     up_soil=None, up_routing=None, execution_details=None, history_feedbacks=None):
    """Bind the hypothesis before MC selection. Non-LLM branches never borrow it."""
    guidance = normalize_guidance(guidance) or {}
    proposal = proposal or {}
    parsed = proposal.get('raw_parsed_proposal') or {}
    eligible = branch in ('llm_proposal', 'worker_proposal_unclassified', 'worker_proposal',
                          'execute_instruction', 'rule_multi_candidate')
    source_payload = parsed if trial_source == 'Worker' else guidance.get('_leader_original', guidance)
    provided_refs = (proposal.get('provided_evidence_refs', []) if trial_source == 'Worker'
                     else guidance.get('_provided_evidence_refs', []))
    audit = audit_trial(source_payload, provided_refs) if eligible else {
        'trial_status': 'no_llm_trial', 'trial_validation_status': 'not_applicable',
        'trial_field_present': False, 'trial': None, 'trial_raw': None,
        'trial_validation_errors': [], 'trial_reference_audit': [],
        'trial_evidence_status': 'no_references',
    }
    trial = audit['trial']
    upstream_context = snapshot({'soil': up_soil or [], 'routing': up_routing or []})
    upstream_id = hashlib.sha256(json.dumps(upstream_context, sort_keys=True, allow_nan=False).encode()).hexdigest()
    anchor = base_record or {}
    anchor_execution = anchor.get('execution_identity') or {}
    anchor_context_id = anchor.get('upstream_context_id')
    anchor_candidate_id = (f"R{anchor.get('round_id')}:{sid}:c{anchor_execution['candidate_id']}"
                           if type(anchor_execution.get('candidate_id')) is int else None)
    record = {
        'protocol': EXPERIMENT_CONTRACT_PROTOCOL,
        'candidate_id': f'R{round_id}:{sid}:c{candidate_number}',
        'round_id': round_id, 'subbasin_id': sid, 'candidate_number': candidate_number,
        'phase': phase, 'branch': branch,
        **audit,
        'trial_source': trial_source if trial is not None else None,
        'trial_attempted_source': trial_source if eligible else None,
        'leader_original_plan': guidance.get('_leader_original'),
        'processed_plan': {'actions': guidance.get('param_adjustments', []),
                           'anti_flip_changes': guidance.get('_anti_flip_changes', []),
                           'radius_policy': 'inactive'},
        'worker_actual_plan': {'reasoning': reasoning, 'trial': trial,
                               'received_context': worker_context,
                               'execution_details': execution_details or {}},
        'proposal_call_id': proposal.get('call_id'), 'proposal_id': proposal.get('proposal_id'),
        'raw_parameter_changes': _parameter_payload(proposal, before_locks),
        'candidate_before_phase_locks': before_locks, 'effective_params': effective_params,
        'simulation_ok': bool(ok), 'metrics': metrics,
        'comparison_anchor': {'round_id': (base_record or {}).get('round_id'),
                              'params': (base_record or {}).get('params'),
                              'metrics': (base_record or {}).get('metrics'),
                              'candidate_id': anchor_candidate_id,
                              'execution_identity': anchor_execution or None,
                              'source': anchor.get('source'),
                              'upstream_context_id': anchor_context_id},
        'upstream_context': upstream_context, 'upstream_context_id': upstream_id,
        'same_upstream_as_anchor': (upstream_id == anchor_context_id) if anchor_context_id else None,
        'acceptance': {'status': 'pending_center_update'},
    }
    return snapshot(record)


def selected_feedback(record, mc_records=None):
    result = copy.deepcopy(record)
    if mc_records and len(mc_records) > 1:
        from .phase_mc_feedback import mc_candidate_feedback, mc_selection_feedback
        result['mc_candidates'] = mc_candidate_feedback(mc_records, record['candidate_id'])
        result['mc_selection'] = mc_selection_feedback(mc_records, record['candidate_id'])
    else:
        result['mc_candidates'] = [
            {'candidate_id': record['candidate_id'], 'NSE': record.get('metrics', {}).get('NSE'),
             'simulation_ok': record.get('simulation_ok'), 'selected': True}]
    return result


def finalize_feedback(feedback, *, accepted, center, reason, static_input_protocol=None):
    if not feedback:
        return
    feedback['acceptance'] = {'status': 'accepted' if accepted else 'rejected',
                              'accepted_as_center': bool(accepted), 'reason': reason}
    feedback['accepted_center_after_round'] = snapshot(center)
    anchor_metrics = (feedback.get('comparison_anchor') or {}).get('metrics') or {}
    current = feedback.get('metrics') or {}
    changes = {}
    checks = []
    finite = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    evidence_status = ('simulation_failed' if feedback.get('simulation_ok') is not True else
                       'unavailable' if not finite(current.get('NSE')) else 'available')
    feedback['metric_evidence_status'] = evidence_status
    for key, value in current.items():
        reference = anchor_metrics.get(key)
        if evidence_status == 'available' and finite(value) and finite(reference):
            changes[key] = value - reference
    for expectation in (feedback.get('trial') or {}).get('expected_response', []):
        key, direction = expectation['metric'], expectation['direction']
        before, after = anchor_metrics.get(key), current.get(key)
        check = {'metric': key, 'expected_direction': direction, 'anchor': before, 'observed': after}
        if evidence_status != 'available':
            check['numerical_match'] = None
            check['status'] = evidence_status
        elif not finite(before) or not finite(after):
            check['numerical_match'] = None
            check['status'] = 'unavailable'
        elif direction == 'unknown':
            check['numerical_match'] = None
            check['status'] = 'unspecified'
        else:
            target = 0 if direction == 'toward_zero' else 1
            matches = {'increase': after > before, 'decrease': after < before,
                       'stable': after == before,
                       'toward_zero': abs(after - target) < abs(before - target),
                       'toward_one': abs(after - target) < abs(before - target)}
            check['numerical_match'] = matches[direction]
            check['status'] = 'observed_numeric_comparison_only'
        checks.append(check)
    feedback['metric_change_from_anchor'] = snapshot(changes)
    feedback['expected_response_checks'] = snapshot(checks)
    feedback['interpretation_limit'] = get_text('experiment_feedback_limit', protocol=static_input_protocol)


def write_contract(round_dir, name, record):
    # Archival failure must not erase a successful physical evaluation or alter MC slots.
    # The existing writer records a critical trace error for the final audit gate.
    from .process_trace import write_json
    path = Path(round_dir) / 'process_trace' / 'experiment_contract' / f'{name}.json'
    return write_json(path, record)
