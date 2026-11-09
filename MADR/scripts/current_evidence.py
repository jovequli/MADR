"""Opt-in, current-reference Worker evidence; no evaluation or LLM calls.

A/B are measured before a proposal. C/D never enter this projection. Existing
round state stays untouched; the same diagnosis is used by LLM and rule fallback.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import re

from .execution_consistency import ExecutionIntegrityError, finite_success
from .worker_diagnostics import diagnose_for_policy

PROTOCOL = 'E_current_reference_v1'


def validate(value=None):
    if value is None:
        return {'protocol': PROTOCOL, 'enabled': False}
    if (not isinstance(value, dict) or set(value) != {'protocol', 'enabled'}
            or value.get('protocol') != PROTOCOL or type(value.get('enabled')) is not bool):
        raise ValueError('Invalid current_evidence_protocol')
    return dict(value)


def enabled(agent):
    return validate(getattr(agent, 'current_evidence_protocol', None))['enabled']


def configure(agent, protocol=None):
    agent.current_evidence_protocol = validate(protocol)
    if not enabled(agent):
        return
    if agent.context_delivery_protocol.get('arm') != 'E_refined':
        raise ValueError('Current-reference evidence requires E_refined delivery')
    old = agent.prompt_source.get_text('current_evidence_system_old_phrase')
    new = agent.prompt_source.get_text('current_evidence_system_phrase')
    if agent.system_prompt.count(old) != 1:
        raise ValueError('Current-reference system wording anchor is missing or ambiguous')
    agent.system_prompt = agent.system_prompt.replace(old, new, 1)
    # configure_static_input() captured the original E system. Preserve its
    # visibility/config metadata, but bind hashes to the system actually sent.
    metadata = deepcopy(getattr(agent, 'static_prompt_metadata', {}))
    metadata.update(agent.prompt_source.provenance_metadata(
        system=agent.system_prompt, user_template=getattr(agent, 'propose_template', None)))
    metadata['current_evidence_protocol'] = dict(agent.current_evidence_protocol)
    agent.static_prompt_metadata = metadata


def _identity(record):
    record = record or {}
    return {k: deepcopy(record.get(k)) for k in (
        'params', 'metrics', 'ok', 'upstream_context_id', 'execution_identity')}


def _same_diagnostic_state(previous, reference):
    # An identical measured state may legitimately have a new evaluation ID.
    from .native_audit import json_safe
    return (bool(previous) and previous.get('ok') is True
            and all(json_safe(previous.get(k)) == json_safe(reference.get(k))
                    for k in ('params', 'metrics', 'upstream_context_id')))


def _upstream_changes(before, configs):
    states = before['states']
    if any(states[k]['status'] != 'available' for k in ('A', 'B')):
        return []
    changes = {}
    for kind, path_key, keys in (
        ('soil', 'soil_base', ('binfilt', 'Ds', 'Dsmax', 'Ws', 'soil_d2', 'soil_d3')),
        ('routing', 'direc_path', ('velocity', 'diffusivity'))):
        old_rows = {row[path_key]: row for row in states['A']['upstream_context'][kind]}
        new_rows = {row[path_key]: row for row in states['B']['upstream_context'][kind]}
        if set(old_rows) != set(new_rows):
            raise ExecutionIntegrityError('Upstream input inventory changed within a run')
        for path, new in new_rows.items():
            old = old_rows[path]
            a = old['params'] if kind == 'soil' else old
            b = new['params'] if kind == 'soil' else new
            delta = {key: [a[key], b[key]] for key in keys if a.get(key) != b.get(key)}
            if not delta:
                continue
            owners = [cfg['subbasin_id'] for cfg in configs if (
                str(Path(cfg.get('soil_base', ''))) == str(Path(path)) if kind == 'soil'
                else bool(cfg.get('rout_template_dir')) and
                Path(cfg['rout_template_dir']) in Path(path).parents)]
            if len(owners) != 1:
                raise ExecutionIntegrityError('Changed upstream input cannot be bound to one station')
            changes.setdefault(owners[0], {}).update(delta)
    return [{'station': sid, 'effective_parameter_changes': params}
            for sid, params in sorted(changes.items())]


def prepare(agent, state, reference, before):
    """Bind immutable, measured pre-proposal facts after the upstream layer commits."""
    if not enabled(agent):
        return None
    agent.current_reference_diagnosis = None
    agent.current_evidence = None
    if state.get('is_final_round') or state['round_id'] == 1:
        return None
    if (not finite_success(reference) or not isinstance(before, dict)
            or before.get('stage') != 'before_proposal'
            or before.get('round_id') != state['round_id']
            or before.get('subbasin_id') != agent.subbasin_id
            or set(before.get('states', {})) != {'A', 'B'}
            or before['states']['B'].get('status') != 'available'):
        raise ExecutionIntegrityError('Current Worker diagnosis requires a valid measured A/B reference')
    b = before['states']['B']
    if any(b.get(k) != reference.get(k) for k in ('params', 'execution_identity', 'upstream_context_id')):
        raise ExecutionIntegrityError('Current diagnosis reference identity mismatch')
    if any(value != (reference.get('metrics') or {}).get(key)
           for key, value in b['metrics'].items() if value is not None):
        raise ExecutionIntegrityError('Current diagnosis reference metrics differ from measured evidence')
    previous = getattr(agent, '_latest_evaluation_record', None)
    prior_diagnosis = getattr(agent, '_last_diagnosis', None)
    reused = prior_diagnosis is not None and _same_diagnostic_state(previous, reference)
    if reused:
        # Preserve headwater/unchanged-background diagnostic strategy and trend.
        diagnosis = deepcopy(prior_diagnosis)
    else:
        ups = state.get('upstream_map', {}).get(agent.subbasin_id, [])
        records = state.get('accepted_center_records', {})
        scores = [records.get(sid, {}).get('metrics', {}).get('NSE') for sid in ups]
        known = bool(ups) and all(finite_success(records.get(sid)) for sid in ups)
        quality = ('poor' if any(score < .5 for score in scores) else 'good') if known else 'unknown'
        diagnosis = diagnose_for_policy(agent.diagnostic_mode, deepcopy(reference['metrics']), None,
            {'is_headwater': not bool(ups), 'upstream_quality': quality, 'simulation_ok': True})
        # Without previous metrics, the default is 'stagnant'; a fresh reference contrast
        # is not evidence of local stagnation or local improvement.
        diagnosis.improvement_trend = 'unknown'
    contrast = before['comparisons']['background']
    background = {'status': contrast['status']}
    if contrast['status'] == 'available':
        background.update(local_parameters_unchanged=True,
            upstream_changed=before['states']['A']['upstream_context_id'] != b['upstream_context_id'],
            NSE_before=before['states']['A']['metrics'].get('NSE'), NSE_current=b['metrics'].get('NSE'),
            delta_NSE=contrast['delta'].get('NSE'))
    payload = {'protocol': PROTOCOL, 'round_id': state['round_id'],
        'station': agent.subbasin_id, 'phase': state['current_phase_name'],
        'reference_identity': _identity(reference),
        'diagnosis_source': 'unchanged_measured_state' if reused else 'current_reference_recomputed',
        'diagnosis': asdict(diagnosis),
        'background': background,
        'upstream_changes': _upstream_changes(before, state.get('worker_cfgs', []))}
    agent.current_reference_diagnosis = diagnosis
    agent.current_evidence = payload
    from .process_trace import write_json
    write_json(Path(state['round_dir'])/'attribution'/f'{agent.subbasin_id}_current_worker_evidence.json', payload)
    return payload


def proposal_diagnosis(agent):
    if not enabled(agent):
        return getattr(agent, '_last_diagnosis', None)
    payload = getattr(agent, 'current_evidence', None)
    current = getattr(agent, 'current_reference_record', None)
    if (not payload or payload['reference_identity'] != _identity(current)
            or getattr(agent, 'current_reference_diagnosis', None) is None):
        raise ExecutionIntegrityError('Worker proposal has stale/missing current diagnostic evidence')
    return agent.current_reference_diagnosis


def complete_reason(reason):
    """Remove only a known code-priority clause and exact duplicate whole clauses.

    Never truncate characters, interpret LLM prose, or delete physical/availability
    qualifications. Unknown/new text is retained verbatim apart from whitespace.
    """
    text = re.sub(r'(?:;\s*)?Legacy heuristic priorities: VIC=\d+%, Routing=\d+%', '', str(reason))
    clauses, seen = [], set()
    for clause in re.split(r';\s+|(?<=[.!?])\s+(?=[A-Z])', text):
        clause = clause.strip()
        identity = clause.rstrip('.')
        if clause and identity not in seen:
            clauses.append(clause)
            seen.add(identity)
    return '; '.join(clauses)


def _sanitize_plan(text, source):
    # E refinement has already separated original and effective action records.
    try:
        plan, end = json.JSONDecoder().raw_decode(text)
    except (ValueError, TypeError):
        return text, False
    changes = plan.get('control_changes', []) if isinstance(plan, dict) else []
    if not changes:
        return text, False
    out = deepcopy(plan)
    changed = False
    for action in out.get('effective_actions', []):
        for change in changes:
            old, new = change.get('original') or {}, change.get('effective') or {}
            if (action.get('param') == new.get('param') and
                    (old.get('direction'), old.get('magnitude')) !=
                    (new.get('direction'), new.get('magnitude'))):
                # Original reasoning remains only under control_changes.original.
                action.pop('reasoning', None)
                action.pop('reason_source', None)
                changed = True
    if changed:
        out['control_interpretation'] = source.get_text('current_evidence_control_note')
    return json.dumps(out, ensure_ascii=False, separators=(',', ':')) + text[end:], changed


def project(agent, blocks):
    """Runs AFTER E rendering, so fixed fields cannot disappear in its whitelist."""
    if not enabled(agent):
        return blocks
    diagnosis = proposal_diagnosis(agent)
    payload = agent.current_evidence
    state = json.loads(blocks['state'])
    if state.get('phase_state', {}).get('round_id') != payload['round_id']:
        raise ExecutionIntegrityError('Rendered Worker round does not match current evidence')
    reference_alias = state.get('proposal_reference')
    if not reference_alias:
        raise ExecutionIntegrityError('Current Worker evidence lacks rendered evaluation identity')
    out = deepcopy(blocks)
    reason = complete_reason(diagnosis.reasoning)
    source = agent.prompt_source
    out.pop('previous_diagnosis', None)
    out['current_reference_diagnosis'] = source.render_static('current_evidence_diagnosis',
        reference=reference_alias, reason=reason)
    background = payload['background']
    if payload['upstream_changes'] or background.get('upstream_changed'):
        facts = {'accepted_upstream_changes': payload['upstream_changes'], 'background': background}
        out['current_upstream_update'] = source.render_static('current_evidence_upstream',
            facts=json.dumps(facts, ensure_ascii=False, separators=(',', ':')))
    shown_plan, changed = _sanitize_plan(out.get('leader_guidance_and_MC_strategy', ''), source)
    out['leader_guidance_and_MC_strategy'] = shown_plan
    agent.current_evidence_projection = {'protocol': PROTOCOL, 'round_id': payload['round_id'],
        'reference_alias': reference_alias, 'diagnosis_source': payload['diagnosis_source'],
        'reason_original_characters': len(diagnosis.reasoning), 'reason_visible_characters': len(reason),
        'reason_truncated': False, 'upstream_update_visible': 'current_upstream_update' in out,
        'control_interpretation_added': changed,
        'block_characters_before': sum(len(v) for v in blocks.values()),
        'block_characters_after': sum(len(v) for v in out.values())}
    return out
