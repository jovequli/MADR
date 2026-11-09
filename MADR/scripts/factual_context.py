"""Runtime facts for diagnostic prompts.

Computes provenance, arithmetic and feasibility only; it does not validate
free-form reasoning, prescribe hydrologic adjustments, or call an LLM."""
from __future__ import annotations

import json
import math
from .prompt_source import get_text, render_static
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

FACTUAL_CONTEXT_PROTOCOL = 'r5.4_factual_context_v1'
METRICS = ('NSE', 'RMSE', 'PBIAS', 'KGE', 'peak_ratio', 'lowflow_ratio',
           'peak_timing_bias_days', 'peak_magnitude_bias_pct', 'recession_k_ratio',
           'baseflow_index_bias_pct')
SOIL = ('binfilt', 'Ds', 'Dsmax', 'Ws', 'soil_d2', 'soil_d3')
ROUTING = ('velocity', 'diffusivity')
DECIMALS = {k: 3 for k in SOIL}
DECIMALS.update(velocity=2, diffusivity=0)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def active_keys(phase):
    phase = getattr(phase, 'value', phase)
    phase = str(phase).upper()
    return SOIL if phase == 'VIC_ONLY' else ROUTING if phase == 'ROUTING_ONLY' else (
        SOIL + ROUTING if phase == 'JOINT' else ())


def phase_facts(round_id, phase, progress=None, point='after_round_evaluation'):
    progress = progress or {}
    name = str(getattr(phase, 'value', phase)).upper()
    history = [h for h in progress.get('phase_history', [])
               if isinstance(h.get('at_round'), int) and h['at_round'] < round_id]
    transitions = [{'from': h.get('from_phase'), 'to': h.get('to_phase'),
                    'completed_through_round': h['at_round']} for h in history]
    last = history[-1] if history and history[-1].get('to_phase') == name else None
    newly_active = (sorted(set(active_keys(name)) - set(active_keys(last['from_phase'])))
                    if last else None)
    phase_round = progress.get('rounds_in_current_phase')
    completed = max(0, phase_round - (point == 'before_worker_proposal')) if (
        isinstance(phase_round, int) and not isinstance(phase_round, bool)) else None
    return {'round_id': round_id, 'phase': name, 'phase_round': phase_round,
            'observation_point': point, 'completed_rounds_in_active_phase': completed,
            'completed_phase_transitions': transitions,
            're_enabled_at_current_phase_start': newly_active,
            'active_parameters': list(active_keys(name)),
            'locked_parameters': [k for k in SOIL + ROUTING if k not in active_keys(name)]}


def objective_facts(round_id, current, history):
    previous_id = round_id - 1 if round_id > 1 else None
    matches = [r for r in (history or []) if r.get('round_id') == previous_id]
    previous = number(matches[-1].get('j_total')) if len(matches) == 1 else None
    current = number(current)
    delta = current - previous if current is not None and previous is not None else None
    return {'direction': 'maximize', 'current_round_id': round_id, 'current': current,
            'previous_round_id': previous_id, 'previous': previous,
            'current_minus_previous': delta,
            'numeric_change': ('unknown' if delta is None else 'increase' if delta > 0 else
                               'decrease' if delta < 0 else 'equal')}


def configured_topology(config):
    workers = config.get('workers', []) if isinstance(config, dict) else []
    if not workers:
        return {}
    edges = {w['subbasin_id']: list(w.get('neighbors_downstream', [])) for w in workers}
    return {sid: {'upstream': [u for u, ds in edges.items() if sid in ds],
                  'downstream': ds,
                  'has_configured_upstream': any(sid in v for v in edges.values())}
            for sid, ds in edges.items()}


def center_slack(center, ranges, phase):
    """Legal single-coordinate grid moves, holding every other parameter fixed.

    Never repair a value here. Coupled depth movement may offer other options;
    this factual block does not change normalization or the executor.
    """
    result = {}
    for key in SOIL + ROUTING:
        pair = ranges.get(key)
        val = number(center.get(key))
        dp = DECIMALS[key]
        step = Decimal(1).scaleb(-dp)
        rec = {'bounds': clean(pair), 'step': float(step), 'center': val,
               'active_in_phase': key in active_keys(phase)}
        if not pair or len(pair) != 2 or any(number(v) is None for v in pair) or val is None:
            rec['status'] = 'unknown'; result[key] = rec; continue
        low = (Decimal(str(pair[0])) / step).to_integral_value(rounding=ROUND_CEILING) * step
        high = (Decimal(str(pair[1])) / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if key in ('soil_d2', 'soil_d3'):
            other = 'soil_d3' if key == 'soil_d2' else 'soil_d2'
            oval = number(center.get(other))
            rec['other_coordinate_held_fixed'] = {other: oval}
            if oval is None:
                rec['status'] = 'unknown_other_depth'; result[key] = rec; continue
            od = Decimal(str(oval))
            if key == 'soil_d2':
                high = min(high, ((od / step).to_integral_value(rounding=ROUND_CEILING)-1) * step)
            else:
                low = max(low, ((od / step).to_integral_value(rounding=ROUND_FLOOR)+1) * step)
        v = Decimal(str(val))
        valid = low <= v <= high and v % step == 0
        rec.update(single_coordinate_interval=[float(low), float(high)], center_is_legal=valid)
        if not valid:
            rec['status'] = 'invalid_center_not_repaired'
        else:
            down, up = int((v-low)/step), int((high-v)/step)
            rec.update(status='known', downward_steps=down, upward_steps=up,
                       downward_distance=float(v-low), upward_distance=float(high-v),
                       downward_steps_in_phase=down if rec['active_in_phase'] else 0,
                       upward_steps_in_phase=up if rec['active_in_phase'] else 0)
        result[key] = rec
    return result


def metric_record(record, source, current_center=None):
    record = record or {}
    metrics = record.get('metrics') or {}
    params = record.get('params', record.get('best_params'))
    ok = record.get('ok') if isinstance(record.get('ok'), bool) else None
    status = str(record.get('status', '')).lower()
    if status in ('failed', 'failure', 'error', 'invalid', 'timeout', 'cancelled', 'canceled'):
        ok = False
    elif ok is None and status in ('success', 'succeeded', 'ok'):
        ok = True
    values = {k: number(metrics.get(k)) if ok is not False else None for k in METRICS}
    result = {'source': source, 'round_id': record.get('round_id'), 'params': clean(params),
              'evidence_stage': ('exploration' if record.get('round_id') == 0 else
                                 'main_loop' if isinstance(record.get('round_id'), int)
                                 and record['round_id'] > 0 else 'unknown'),
              'evaluation_ok': ok, 'metrics': values,
              'unavailable_metrics': [k for k, v in values.items() if v is None]}
    for k in ('evaluation_start', 'evaluation_end', 'n_scored_days', 'diagnostic_status'):
        if k in metrics:
            result[k] = clean(metrics[k])
    for key in ('execution_identity', 'upstream_context_id', 'observed_round_id',
                'evaluation_round_id', 'phase', 'result_scope', 'provenance_status',
                'accepted_record_source', 'accepted_as_center_current_round'):
        if key in record:
            result[key] = clean(record[key])
    result['local_params_equal_to_center'] = (params == current_center if isinstance(params, dict)
                                              and current_center else None)
    return result



def leader_accepted_metric_record(record, observed_round_id, current_center, terminal=False):
    """Describe the measured accepted state without relabelling a rejected proposal.

    Native execution identity is authoritative. An observation of an unchanged
    center does not imply a fresh model call in the currently reviewed round.
    """
    import re
    record = record or {}
    execution = record.get('execution_identity') or {}
    source_round = None
    is_reference = False
    if isinstance(execution, dict):
        run_id, candidate_id = execution.get('run_id'), execution.get('candidate_id')
        tag = execution.get('phase_tag')
        match = re.fullmatch(r'center_reference_round_(\d+)', tag) if isinstance(tag, str) else None
        if match:
            tagged_round = int(match.group(1))
            is_reference = True
            tag_candidate = 0
        else:
            match = re.fullmatch(r'round_(\d+)_(?:c(\d+)|mc(\d+)_.+)', tag) if isinstance(tag, str) else None
            tagged_round = int(match.group(1)) if match else None
            tag_candidate = int(match.group(2) or match.group(3)) if match else None
        if (type(run_id) is int and type(candidate_id) is int and match
                and run_id == tagged_round * 100 + candidate_id
                and candidate_id == tag_candidate
                and 1 <= tagged_round <= observed_round_id):
            source_round = tagged_round
    valid_identity = source_round is not None
    if not valid_identity:
        source = 'unresolved_accepted_state_provenance'
    elif terminal:
        source = 'terminal_joint_confirmation' if source_round == observed_round_id else 'retained_measured_center'
    elif is_reference:
        source = 'same_background_center_reference'
    elif record.get('accepted_as_center') is True and source_round == observed_round_id:
        source = 'accepted_candidate'
    elif record.get('accepted_as_center') is False or source_round < observed_round_id:
        source = 'retained_measured_center'
    else:
        source = 'measured_joint_state'
    measured = dict(record, round_id=source_round,
                    params=record.get('best_params', record.get('params')))
    facts = metric_record(measured, source, current_center)
    facts.update({
        'observed_round_id': observed_round_id,
        'evaluation_round_id': source_round,
        'execution_identity': clean(execution),
        'result_scope': 'accepted_joint_state',
        'accepted_as_center_current_round': record.get('accepted_as_center'),
        'accepted_record_source': record.get('accepted_record_source'),
        'provenance_status': 'native_identity_available' if valid_identity else 'unresolved',
    })
    return facts

def resolve_worker_metric_record(metrics, explicit_record=None, history=None):
    """Bind only a supplied matching source or an unambiguous recorded match."""
    def matches(r):
        return bool(metrics) and number(metrics.get('NSE')) is not None and clean(r.get('metrics')) == clean(metrics)
    if explicit_record and matches(explicit_record):
        return metric_record(explicit_record, explicit_record.get('source') or 'explicit_measured_record')
    found = [r for r in (history or []) if matches(r)]
    identities = {(r.get('round_id'), json.dumps(clean(r.get('params')), sort_keys=True)) for r in found}
    if len(identities) == 1:
        return metric_record(found[0], 'unique_history_match')
    return metric_record({'metrics': metrics}, 'unresolved_metric_provenance')


def leader_facts(round_id, phase, progress, worker_results, centers, ranges, config, current_j, history):
    progress = progress or {}
    observed_phase = progress.get('observed_phase', phase)
    target_phase = progress.get('decision_target_phase', phase)
    purpose = progress.get('decision_purpose', 'next_round_guidance')
    target_active = () if purpose == 'terminal_review_only' else active_keys(target_phase)
    target = {
        'round_id': progress.get('decision_target_round_id', round_id + 1),
        'phase': str(getattr(target_phase, 'value', target_phase)).upper(),
        'round_in_phase': progress.get('decision_target_round_in_phase'),
        'purpose': purpose,
        'active_parameters': list(target_active),
        'locked_parameters': [k for k in SOIL + ROUTING if k not in target_active],
    }
    topology = configured_topology(config)
    stations = {}
    by_station = {wr['subbasin_id']: wr for wr in worker_results}
    missing = sorted(set(topology) - set(by_station))
    for sid in sorted(set(topology) | set(by_station)):
        center = centers.get(sid, {})
        wr = by_station.get(sid)
        # The graph supplies the accepted joint state; its actual model evidence
        # may be an older retained center or a current downstream reference.
        evidence = (leader_accepted_metric_record(wr, round_id, center,
                        terminal=purpose == 'terminal_review_only') if wr else
                    metric_record({}, 'missing_current_round_result', center))
        stations[sid] = {'topology': topology.get(sid),
                         'latest_evaluation': evidence,
                         'accepted_proposal_center': clean(center),
                         'center_legal_single_coordinate_moves': center_slack(
                             center, ranges, target_phase if target_active else None)}
    return {'protocol': FACTUAL_CONTEXT_PROTOCOL,
            'phase_state': phase_facts(round_id, observed_phase, progress),
            'decision_target': target,
            'J_total': objective_facts(round_id, current_j, history),
            'missing_configured_stations': missing, 'stations': stations}


def worker_facts(round_id, phase, progress, sid, center, metrics, explicit_record, history,
                 latest_record, ranges, config):
    source = resolve_worker_metric_record(metrics, explicit_record, history)
    source['local_params_equal_to_center'] = (source['params'] == center
                                             if isinstance(source['params'], dict) and center else None)
    return {'protocol': FACTUAL_CONTEXT_PROTOCOL,
            'phase_state': phase_facts(round_id, phase, progress, 'before_worker_proposal'),
            'station': sid, 'topology': configured_topology(config).get(sid),
            'supplied_metric_record': source,
            'latest_evaluation': metric_record(latest_record, 'last_diagnostic_evaluation', center),
            'accepted_proposal_center': clean(center),
            'center_legal_single_coordinate_moves': center_slack(center, ranges, phase)}


def render(facts, static_input_protocol=None):
    """Compact prompt view; detailed dictionaries remain available for auditing.

    Metric values already appear in source-labelled evidence sections. Repeat
    identities and unavailable fields here, not a second full metric table.
    """
    def identity(record):
        result = {k: record.get(k) for k in (
            'source', 'round_id', 'evidence_stage', 'params', 'evaluation_ok',
            'unavailable_metrics', 'local_params_equal_to_center',
            'observed_round_id', 'evaluation_round_id', 'execution_identity',
            'result_scope', 'accepted_as_center_current_round', 'accepted_record_source',
            'provenance_status') if k in record}
        return result

    def station_view(station):
        moves = station['center_legal_single_coordinate_moves']
        slack = {key: ([m['downward_steps'], m['upward_steps']] if m.get('status') == 'known'
                       else {'status': m.get('status')}) for key, m in moves.items()}
        return {'topology': station.get('topology'),
                'latest_evaluation': identity(station['latest_evaluation']),
                'accepted_proposal_center': station['accepted_proposal_center'],
                'center_move_steps_down_up': slack}

    view = {'protocol': facts['protocol'], 'phase_state': facts['phase_state']}
    if 'stations' in facts:
        view['decision_target'] = facts['decision_target']
        view['J_total'] = facts['J_total']
        view['missing_configured_stations'] = facts['missing_configured_stations']
        view['stations'] = {sid: station_view(station) for sid, station in facts['stations'].items()}
        first = next(iter(facts['stations'].values()), {})
        moves = first.get('center_legal_single_coordinate_moves', {})
    else:
        view['station'] = facts['station']
        view['supplied_metric_record'] = identity(facts['supplied_metric_record'])
        view.update(station_view(facts))
        moves = facts['center_legal_single_coordinate_moves']
    view['runtime_parameter_grid'] = {
        key: {'bounds': m.get('bounds'), 'step': m['step']} for key, m in moves.items()}
    phase_note = get_text('facts_leader_phase_note' if 'stations' in facts else
                          'facts_worker_phase_note', protocol=static_input_protocol)
    return render_static('facts_runtime_evidence', protocol=static_input_protocol,
                         facts_json=json.dumps(clean(view), ensure_ascii=False,
                                               separators=(',', ':'), allow_nan=False),
                         phase_note=phase_note,
                         evidence_rules=get_text('facts_evidence_use_rules', protocol=static_input_protocol))
