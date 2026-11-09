"""Opt-in Worker message projection; no parameter, scoring or policy changes.

Complete objects stay in the existing trace. This module never truncates history,
rounds numbers, generates a hypothesis, or mutates input blocks/Records.
"""
from __future__ import annotations

import copy
import json


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _object(text, default=None):
    if not text:
        return copy.deepcopy(default)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return copy.deepcopy(default)


def _pick(value, keys):
    return {k: copy.deepcopy(value[k]) for k in keys if k in value}


def _trial(value):
    """Keep the prior hypothesis and uncertainty, without reference aliases."""
    if not isinstance(value, dict):
        return None
    return _pick(value, ('hypothesis', 'uncertainty', 'expected_response'))


def _action(value):
    if not isinstance(value, dict):
        return None
    shown = _pick(value, ('param', 'direction', 'magnitude', 'coupled_group'))
    reason = value.get('reasoning', value.get('reason'))
    if reason is not None:
        shown['reasoning'] = copy.deepcopy(reason)
    return shown


def _control_changes(plan):
    """One record per control alteration, independent of duplicate audit wrappers."""
    changes = []
    seen = set()
    # anti_flip has the specific reason; prefer it to the generic before/after copy.
    for key in ('anti_flip_changes', 'control_changes', 'phase_excluded_actions'):
        for source in plan.get(key, []) or []:
            if not isinstance(source, dict):
                continue
            old = _action(source.get('original'))
            new = _action(source.get('processed', source.get('effective')))
            if key == 'phase_excluded_actions' and old is None:
                old = _action(source.get('action', source))
            if old is None and new is None:
                continue
            identity = _json({'original': old, 'effective': new})
            if identity in seen:
                continue
            seen.add(identity)
            row = {'original': old, 'effective': new}
            if source.get('reason'):
                row['control_reason'] = copy.deepcopy(source['reason'])
            elif key == 'phase_excluded_actions':
                row['control_reason'] = 'excluded by phase control'
            # An unchanged reason belongs to the original hypothesis, not a new
            # physical justification for a code-altered direction/magnitude.
            if old and new and old.get('reasoning') == new.get('reasoning'):
                new.pop('reasoning', None)
            changes.append(row)
    return changes


def _feedback(value):
    if not isinstance(value, dict):
        return None
    out = _pick(value, ('round_id', 'phase', 'selected_candidate', 'comparison_anchor',
                        'accepted_center_after_round', 'same_upstream_as_anchor', 'mc_candidates', 'mc_selection', 'mc_parameter_columns', 'mc_upstream',
                        'candidate_proposal_reasoning'))
    acceptance = value.get('acceptance')
    if isinstance(acceptance, dict):
        # Accepted/rejected and model success are distinct. Evaluation success is
        # retained in the referenced record, not inferred from this acceptance.
        out['acceptance'] = _pick(acceptance, ('accepted_as_center', 'status', 'reason'))
    trial = _trial(value.get('trial'))
    if trial:
        out['prior_hypothesis'] = trial
    checks = value.get('expectation_checks')
    if isinstance(checks, list):
        out['observed_expectation_checks'] = [_pick(x, ('metric', 'expected_direction', 'numerical_match'))
                                             for x in checks if isinstance(x, dict)]
    changes = _control_changes(value)
    if changes:
        out['previous_control_changes'] = changes
    return out


def _plan(text):
    """Preserve all effective actions and model wording; remove repeated wrappers."""
    prefix = 'Effective Leader guidance:\n'
    if not isinstance(text, str) or not text.startswith(prefix):
        return text
    content = text[len(prefix):]
    try:
        value, end = json.JSONDecoder().raw_decode(content)
    except (TypeError, ValueError):
        # Unknown future presentation stays visible, rather than silently lost.
        return text
    if not isinstance(value, dict):
        return text
    strategy = content[end:].strip()
    out = _pick(value, ('phase', 'priority_focus', 'guidance_target'))
    changes = _control_changes(value)
    effective = [_action(a) for a in value.get('effective_actions', []) if isinstance(a, dict)]
    for action in effective:
        for change in changes:
            applied = change.get('effective')
            original = change.get('original')
            if (applied and original and action.get('param') == applied.get('param')
                    and action.get('direction') == applied.get('direction')
                    and action.get('magnitude') == applied.get('magnitude')
                    and action.get('reasoning') == original.get('reasoning')):
                action.pop('reasoning', None)
                action['reason_source'] = 'control_changes.original'
                break
    out['effective_actions'] = effective
    if changes:
        out['control_changes'] = changes
    trial = _trial(value.get('trial'))
    if trial:
        out['leader_hypothesis'] = trial
    return _json(out) + ('\n' + strategy if strategy else '')


def refine_guidance_text(text):
    """Public offline/replay entry for the same Worker guidance projection."""
    return _plan(text)


def refine_worker_blocks(agent, blocks, records):
    """Return a new E-only display mapping; archival inputs remain unchanged."""
    from .refined_common import refine_records
    shown = copy.deepcopy(blocks)
    state = _object(blocks.get('state'))
    if not isinstance(state, dict):
        return shown
    phase = state.get('phase_state') or {}
    slim = _pick(state, ('station', 'accepted_center', 'proposal_reference',
                        'previous_diagnostic_evaluation', 'same_upstream_reference_vs_previous_diagnostic',
                        'historical_best_observed_NSE', 'topology', 'parameter_grid'))
    slim['phase_state'] = _pick(phase, ('round_id', 'phase', 'phase_round',
                                      'active_parameters', 'locked_parameters'))
    if phase.get('re_enabled_at_current_phase_start'):
        slim['phase_state']['re_enabled_at_current_phase_start'] = copy.deepcopy(phase['re_enabled_at_current_phase_start'])
    moves = state.get('legal_single_coordinate_steps_down_up')
    if isinstance(moves, dict):
        active = phase.get('active_parameters')
        slim['active_coordinate_steps_down_up'] = {k: copy.deepcopy(v) for k, v in moves.items()
                                                  if not isinstance(active, list) or k in active}
    shown['task'] = 'Propose legal parameter changes to maximize evaluation-period NSE; omitted parameters retain the accepted center.'
    shown['state'] = _json(slim)
    body = _object(blocks.get('records'))
    if isinstance(body, dict):
        shown['records'] = _json(refine_records(copy.deepcopy(body)))
    label = 'Previous completed-evaluation diagnosis (not a diagnosis of a new reference unless the evaluation identity matches):\n'
    previous = blocks.get('previous_diagnosis', '')
    if isinstance(previous, str) and previous.startswith(label):
        reference = state.get('previous_diagnostic_evaluation')
        shown['previous_diagnosis'] = f'Diagnosis of {reference if reference is not None else "unavailable previous evaluation"}:\n' + previous[len(label):]
    try:
        feedback = json.loads(blocks.get('selected_candidate_feedback', 'null'))
    except (TypeError, ValueError):
        pass  # Preserve unknown future text; do not manufacture an unavailable record.
    else:
        if feedback is None or isinstance(feedback, dict):
            shown['selected_candidate_feedback'] = _json(_feedback(feedback))
    shown['leader_guidance_and_MC_strategy'] = refine_guidance_text(blocks.get('leader_guidance_and_MC_strategy', ''))
    shown['record_legend'] = agent.prompt_source.get_text('refined_record_legend')
    # Internal validation still uses agent.experiment_evidence_refs. Duplicate
    # printable allow-lists are not new hydrological evidence.
    shown.pop('evidence_refs', None)
    # In particular history and exploration are deliberately byte-for-byte kept.
    return shown
