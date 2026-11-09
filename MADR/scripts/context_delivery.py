"""Model-facing projections only; complete runtime objects remain authoritative.

Projects static knowledge and runtime evidence into agent inputs.
There is no model call, persistent conversation, numerical rule or truncation here.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from .factual_context import (clean, METRICS, active_keys, worker_facts, leader_facts)
from .experiment_contract import evidence_index, snapshot

PROTOCOL = 'minimal_context_delivery_v1'
ARMS = ('E_refined',)


def validate_context_delivery_protocol(value=None):
    value = {'protocol': PROTOCOL, 'arm': 'E_refined'} if value is None else value
    if (not isinstance(value, dict) or set(value) != {'protocol', 'arm'}
            or value.get('protocol') != PROTOCOL or value.get('arm') not in ARMS):
        raise ValueError('Invalid context_delivery_protocol: exact protocol and supported arm required')
    return dict(value)


def validate_context_config(config, method='MADR'):
    from .context_efficiency import validate_options
    validate_options(config.get('context_efficiency'))
    result = validate_context_delivery_protocol(config.get('context_delivery_protocol'))
    if method not in ('MADR','SADR'):
        raise ValueError('Unknown method for context delivery')
    if 'context_delivery_protocol' not in config:
        return result  # Configurations without an explicit protocol use the default projection.
    static = config.get('static_input_protocol', {})
    if (static.get('storage') != 'markdown' or static.get('worker_content') != 'minimal'
            or config.get('worker_evidence_mode', 'existing') != 'existing'
            or config.get('diagnostic_protocol', {}).get('mode') != 'historical_policy'):
        raise ValueError('Context delivery requires markdown minimal / existing / historical_policy')
    return result


def _json(value):
    return json.dumps(clean(snapshot(value)), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def render_context_system(prompt_source, role, visibility_protocol=None, context_delivery_protocol=None):
    """System prompt renderer used by both agents and preflight, with MD-only wording."""
    if role not in ('worker', 'leader'):
        raise ValueError('Unknown prompt role')
    validate_context_delivery_protocol(context_delivery_protocol)
    from .refined_common import refined_output_contract
    if prompt_source.protocol.get('worker_content') != 'minimal':
        raise ValueError('Refined delivery requires the minimal knowledge source')
    return '\n\n'.join((prompt_source.get_text('refined_' + role + '_system'),
                         refined_output_contract(role, prompt_source)))


def _without_audit(value):
    """Remove only program-owned private audit keys; retain model reasoning."""
    if isinstance(value, dict):
        return {k: _without_audit(v) for k, v in value.items() if not k.startswith('_')}
    if isinstance(value, list):
        return [_without_audit(v) for v in value]
    return snapshot(value)


def _prior_trial(trial, source_request):
    """Qualify display-local refs copied from an earlier/different request.

    The saved model annotation is unchanged. E1/P1/etc in a past trial must not
    be silently rebound to this prompt's independently numbered record pool.
    Persistent candidate IDs and metric/source labels retain their original text.
    """
    if not isinstance(trial, dict):
        return snapshot(trial)
    shown = snapshot(trial)
    shown['evidence_refs'] = [source_request + '::' + ref
        if isinstance(ref, str) and re.fullmatch(r'[EPMU]\d+', ref) else ref
        for ref in shown.get('evidence_refs', [])]
    return shown


def _prior_annotation(value, source_request):
    """Qualify copied code-control annotations without rebinding old aliases."""
    if isinstance(value, list):
        return [_prior_annotation(item, source_request) for item in value]
    if isinstance(value, dict):
        return {key: [(source_request + '::' + ref
            if isinstance(ref, str) and re.fullmatch(r'[EPMU]\d+', ref) else ref)
            for ref in item] if key == 'evidence_refs' and isinstance(item, list)
            else _prior_annotation(item, source_request) for key, item in value.items()}
    return snapshot(value)


def compact_worker_plan(processed, phase):
    original = processed.get('_leader_original', {})
    effective = _without_audit(processed.get('param_adjustments', []))
    initial = _without_audit(original.get('param_adjustments', []))
    # Normalization adds empty refs and a reasoning alias. Canonicalizing these
    # avoids describing a mere representation change as a policy intervention.
    def canonical(actions):
        out = []
        for action in actions:
            a = dict(action)
            a['reasoning'] = a.get('reasoning') or a.pop('reason', '')
            a.pop('reason', None)
            a['evidence_refs'] = [('leader_request::' + ref if isinstance(ref, str) and re.fullmatch(r'[EPMU]\d+', ref) else ref)
                                  for ref in a.get('evidence_refs', [])]
            out.append(a)
        return out
    effective, initial = canonical(effective), canonical(initial)
    body = {'phase': phase, 'priority_focus': processed.get('priority_focus', ''),
            'effective_actions': effective, 'trial': _prior_trial(processed.get('trial'), 'leader_request'),
            'trial_reference_scope': 'original Leader request; display-local aliases are not current Worker records'}
    identity = processed.get('_guidance_identity')
    if isinstance(identity, dict):
        body['guidance_target'] = {k: v for k, v in identity.items()
                                   if k in ('source_round_id', 'target_round_id', 'target_phase', 'purpose',
                                            'observed_round_id', 'decision_target_round_id',
                                            'observed_phase', 'decision_target_phase', 'decision_purpose')}
    if initial != effective:
        changes = []
        # Retain full changed/excluded actions, rather than the entire unchanged plan.
        for index, old in enumerate(initial):
            new = effective[index] if index < len(effective) else None
            if old != new:
                changes.append({'original': old, 'effective': new})
        for new in effective[len(initial):]:
            changes.append({'original': None, 'effective': new})
        body['control_changes'] = changes
    if processed.get('_anti_flip_changes'):
        body['anti_flip_changes'] = _prior_annotation(_without_audit(processed['_anti_flip_changes']), 'leader_request')
    if processed.get('_phase_excluded_actions'):
        body['phase_excluded_actions'] = _prior_annotation(_without_audit(processed['_phase_excluded_actions']), 'leader_request')
    # Changed/excluded hypotheses are not presented as if their intended action ran.
    return 'Effective Leader guidance:\n' + _json(body)


class Records:
    """Typed value dictionaries; evaluation roles never merge on params alone.

    Exact parameter and measured-metric values can be referenced repeatedly.
    Evaluations merge only if their full native identity *and* content match.
    Unresolved evaluations remain distinct. Complete identities stay in the audit.
    """
    def __init__(self):
        self.parameters = {}; self.measurements = {}; self.evaluations = {}
        self._params = {}; self._metrics = {}; self._evals = {}; self._upstream = {}
        self.audit = {'upstream_ids': {}, 'evaluation_identities': {}}

    def _value(self, value, table, index, prefix):
        if not isinstance(value, dict):
            return None
        value = clean(snapshot(value))
        key = _json(value)
        if key not in index:
            ref = prefix + str(len(table) + 1)
            table[ref] = value; index[key] = ref
        return index[key]

    def params(self, value):
        return self._value(value, self.parameters, self._params, 'P')

    def metrics(self, value, ok=None):
        if not isinstance(value, dict):
            return None
        measured = dict(value)
        for key in METRICS:
            measured.setdefault(key, None)
        if ok is False:
            # Preserve availability/status, not a stale failed score as usable evidence.
            measured = {k: (None if isinstance(v, (int, float)) else v) for k, v in measured.items()}
        return self._value(measured, self.measurements, self._metrics, 'M')

    def upstream(self, full_id):
        if not full_id:
            return None
        if full_id not in self._upstream:
            ref = 'U' + str(len(self._upstream) + 1)
            self._upstream[full_id] = ref; self.audit['upstream_ids'][ref] = full_id
        return self._upstream[full_id]

    def evaluation(self, record, *, params=None, metrics=None, ok=None):
        record = record or {}
        params = record.get('params', record.get('best_params')) if params is None else params
        metrics = record.get('metrics') if metrics is None else metrics
        ok = record.get('evaluation_ok', record.get('simulation_ok', record.get('ok'))) if ok is None else ok
        native = record.get('execution_identity')
        identity = {'execution_identity': native, 'upstream_context_id': record.get('upstream_context_id'),
                    'candidate_id': record.get('candidate_id'), 'round_id': record.get('round_id')}
        body = {k: clean(record[k]) for k in (
            'round_id', 'evaluation_round_id', 'observed_round_id', 'candidate_id', 'source',
            'evidence_stage', 'result_scope', 'provenance_status', 'accepted_record_source',
            'accepted_as_center_current_round', 'phase') if k in record}
        body.update(params=self.params(params), metrics=self.metrics(metrics, ok),
                    evaluation_ok=ok, upstream=self.upstream(record.get('upstream_context_id')))
        # Only meaningful, compact execution coordinates go online; not long hashes/paths.
        if isinstance(native, dict):
            coordinates = {k: native[k] for k in ('run_id', 'candidate_id', 'phase_tag') if k in native}
            if coordinates:
                body['execution'] = coordinates
        full_key = _json({'identity': identity, 'body': body})
        known = bool(native or record.get('candidate_id'))
        if known and full_key in self._evals:
            return self._evals[full_key]
        ref = 'E' + str(len(self.evaluations) + 1)
        self.evaluations[ref] = body; self.audit['evaluation_identities'][ref] = snapshot(identity)
        if known:
            self._evals[full_key] = ref
        return ref

    def body(self):
        return {'parameters': self.parameters, 'measurements': self.measurements,
                'evaluations': self.evaluations}


def compact_feedback(feedback, records, include_reasoning=False):
    if not feedback:
        return None
    anchor = feedback.get('comparison_anchor') or {}
    output = {k: snapshot(feedback[k]) for k in (
        'candidate_id', 'round_id', 'subbasin_id', 'phase', 'branch',
        'trial_status', 'trial_source', 'trial_validation_status', 'trial_evidence_status',
        'trial', 'metric_evidence_status', 'acceptance', 'same_upstream_as_anchor',
        'mc_candidates', 'mc_selection') if k in feedback}
    mc_rows = output.get('mc_candidates', [])
    if len(mc_rows) > 1 and all('effective_active_params' in row for row in mc_rows):
        columns = list(active_keys(feedback.get('phase')))
        output['mc_parameter_columns'] = columns
        for row in mc_rows:
            params = row.pop('effective_active_params')
            row['effective_values'] = [params.get(key) for key in columns]
        backgrounds = [row.get('upstream_context_id') for row in mc_rows]
        shared_background = backgrounds[0] is not None and all(value == backgrounds[0] for value in backgrounds)
        if shared_background:
            output['mc_upstream'] = records.upstream(backgrounds[0])
            for row in mc_rows:
                row.pop('upstream_context_id', None)
    for candidate in mc_rows:
        if 'upstream_context_id' in candidate:
            candidate['upstream'] = records.upstream(candidate.pop('upstream_context_id'))
    if output.get('trial') is not None:
        output['trial'] = _prior_trial(output['trial'], 'proposal_request')
        output['trial_reference_scope'] = 'candidate proposal request; aliases are not current-request records'
    output['selected_candidate'] = records.evaluation(feedback,
        params=feedback.get('effective_params'), ok=feedback.get('simulation_ok'))
    output['comparison_anchor'] = records.evaluation(anchor)
    output['accepted_center_after_round'] = records.params(feedback.get('accepted_center_after_round'))
    # Checks refer to the two metric records instead of duplicating all observations.
    if feedback.get('expected_response_checks'):
        output['expectation_checks'] = [{k: check[k] for k in
            ('metric', 'expected_direction', 'numerical_match', 'status') if k in check}
            for check in feedback['expected_response_checks']]
    if feedback.get('metric_change_from_anchor'):
        output['metric_delta_from_anchor'] = {k: v for k, v in feedback['metric_change_from_anchor'].items()
                                             if k in METRICS}
    changes = feedback.get('processed_plan', {}).get('anti_flip_changes', [])
    if changes:
        output['control_changes'] = _prior_annotation(_without_audit(changes), 'leader_request')
    if include_reasoning:
        reason = (feedback.get('worker_actual_plan') or {}).get('reasoning')
        if reason:
            output['candidate_proposal_reasoning'] = snapshot(reason)
    return output


def _slack(moves):
    return {key: [m['downward_steps'], m['upward_steps']]
            if m.get('status') == 'known' else {'status': m.get('status')}
            for key, m in moves.items()}


def _render(agent, blocks, records, refs):
    """Refine the blocks for the agent role and join them into the user prompt."""
    source_blocks = copy.deepcopy(blocks)
    if hasattr(agent, 'propose_template'):
        from .refined_worker import refine_worker_blocks
        blocks = refine_worker_blocks(agent, blocks, records)
        from .current_evidence import project as project_current_evidence
        blocks = project_current_evidence(agent, blocks)
    else:
        from .refined_leader import refine_leader_blocks
        blocks = refine_leader_blocks(agent, blocks, records)
    from .context_efficiency import project
    blocks, efficiency_audit = project(agent, blocks)
    order = list(blocks)
    agent.experiment_evidence_refs = sorted(set(refs) | set(records.evaluations) | set(records.parameters)
                                            | set(records.measurements) | set(records.audit['upstream_ids']))
    prompt = '\n\n'.join('## ' + name + '\n' + blocks[name] for name in order if blocks[name])
    metadata = agent.prompt_source.provenance_metadata(system=agent.system_prompt,
        user_template=agent.propose_template if hasattr(agent, 'propose_template') else agent.review_template)
    from .prompt_visibility import visibility_metadata
    metadata.update(visibility_metadata(agent.prompt_visibility_protocol))
    metadata['context_delivery_protocol'] = dict(agent.context_delivery_protocol)
    metadata['context_delivery'] = {'block_order': order,
        'block_chars': {k: len(v) for k, v in blocks.items()},
        'block_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in blocks.items()},
        'identity_map': records.audit, 'input_characters': len(agent.system_prompt) + len(prompt),
        'projection_scope': 'model-facing only; complete source objects are archived unchanged'}
    metadata['context_efficiency'] = efficiency_audit
    metadata['context_refinement'] = {
        'protocol': 'minimal_compact_refinement_v1',
        'static_rule_table_visible': False,
        'dynamic_diagnosis_visibility': 'Diagnosis labels and reasoning retained',
        'solicit_structured_trial': False,
        'source_block_chars': {k: len(v) for k, v in source_blocks.items()},
        'source_block_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in source_blocks.items()},
        'source_blocks_archive_only': source_blocks,
    }
    from .current_evidence import enabled as current_evidence_enabled
    if hasattr(agent, "propose_template") and current_evidence_enabled(agent):
        metadata["current_evidence_protocol"] = dict(agent.current_evidence_protocol)
        metadata["current_evidence"] = copy.deepcopy(agent.current_evidence_projection)
        metadata["context_refinement"]["dynamic_diagnosis_visibility"] = "Current measured reference, complete diagnosis clauses, archived previous diagnosis retained as history"
    agent.static_prompt_metadata = metadata
    return prompt


def build_worker_prompt(agent, round_id, center, metrics, phase, leader_context=''):
    from .historical_policy import policy_worker_lines
    history = getattr(agent.memory, 'history', [])
    latest = getattr(agent, '_latest_evaluation_record', None)
    facts = worker_facts(round_id, phase, getattr(agent, 'factual_runtime_progress', {}),
        agent.subbasin_id, center, metrics, getattr(agent, 'factual_metric_record', None),
        history, latest, agent.ranges, getattr(agent, 'factual_runtime_config', {}))
    records = Records()
    supplied = facts['supplied_metric_record']
    latest_fact = facts['latest_evaluation']
    supplied_ref = records.evaluation(supplied, metrics={**metrics, **supplied['metrics']})
    latest_ref = (records.evaluation(latest_fact,
                    metrics={**((latest or {}).get('metrics') or {}), **latest_fact['metrics']})
                  if latest else None)
    feedback = history[-1].get('experiment_feedback') if history else None
    feedback_view = compact_feedback(feedback, records,
        include_reasoning=True)
    known_upstream = supplied.get('upstream_context_id') and latest_fact.get('upstream_context_id')
    state = {'station': agent.subbasin_id, 'station_code': agent.station_code,
        'phase_state': facts['phase_state'], 'topology': facts['topology'],
        'accepted_center': records.params(center), 'proposal_reference': supplied_ref,
        'previous_diagnostic_evaluation': latest_ref,
        'same_upstream_reference_vs_previous_diagnostic':
            supplied.get('upstream_context_id') == latest_fact.get('upstream_context_id') if known_upstream else None,
        'legal_single_coordinate_steps_down_up': _slack(facts['center_legal_single_coordinate_moves'])}
    if not getattr(agent, 'context_runtime_in_system', False):
        state['parameter_grid'] = {k: {'bounds': v['bounds'], 'step': v['step']}
                                    for k, v in facts['center_legal_single_coordinate_moves'].items()}
    best = agent.ledger.get_best_params()
    state['historical_best_observed_NSE'] = best.get('nse') if best else None
    diagnosis = agent._last_diagnosis if latest_fact.get('evaluation_ok') is not False else None
    diag_lines = policy_worker_lines(diagnosis, False, agent.static_input_protocol,
                                         agent.prompt_visibility_protocol) if latest else ['Unavailable.']
    blocks = {
        'task': agent.prompt_source.get_text('context_worker_request'),
        'state': _json(state),
        'records': _json(records.body()),
        'previous_diagnosis': agent.prompt_source.get_text('context_worker_diagnosis_label') + '\n' + '\n'.join(diag_lines),
        'selected_candidate_feedback': _json(feedback_view),
        'history': agent.memory.format_full_history_table(phase=str(getattr(phase, 'value', phase)).upper(), exclude_latest_mc=True),
        'exploration': getattr(agent, '_exploration_summary', '') or '',
        'record_legend': agent.prompt_source.get_text('context_record_legend'),
        'leader_guidance_and_MC_strategy': leader_context,
    }
    refs = evidence_index(metrics, [feedback], ('accepted_center', supplied.get('source', 'unresolved_metric_provenance'))
                         + (('last_diagnostic_evaluation',) if latest else ()))
    return _render(agent, blocks, records, refs)


def build_leader_prompt(agent, round_id, results, centers, phase, phase_round,
                        j_total, outlet_nse, mean_sub_nse, diagnostic_text=None):
    from .historical_policy import policy_leader_lines
    progress = dict(getattr(agent, 'factual_runtime_progress', {}))
    progress.setdefault('rounds_in_current_phase', phase_round)
    facts = leader_facts(round_id, phase, progress, results, centers, agent.ranges,
        getattr(agent, 'factual_runtime_config', {'workers': agent.workers}), j_total, agent.memory.rounds)
    records = Records(); reports = getattr(agent, 'context_diagnostic_reports', [])
    by_sid = {r['subbasin_id']: r for r in reports}
    stations = {}; feedbacks = [r.get('experiment_feedback') for r in results]
    results_by_sid = {r['subbasin_id']: r for r in results}
    for sid, fact in facts['stations'].items():
        measured = fact['latest_evaluation']; actual = results_by_sid.get(sid, {})
        report = by_sid.get(sid)
        row = {'accepted_center': records.params(fact['accepted_proposal_center']),
            'accepted_joint_evaluation': records.evaluation(measured,
                metrics={**(actual.get('metrics') or {}), **measured['metrics']}),
            'legal_single_coordinate_steps_down_up': _slack(fact['center_legal_single_coordinate_moves']),
            'selected_candidate_feedback': compact_feedback(actual.get('experiment_feedback'), records,
                include_reasoning=True)}
        if report:
            failed = report.get('simulation_ok') is False
            rs = report.get('round_summary', {})
            row['round_summary'] = ('unavailable: failed evaluation' if failed else {
                'nse_delta': rs.get('nse_delta'), 'timing_status': rs.get('timing_status'),
                'recession_status': rs.get('recession_status'), 'is_stagnant': rs.get('is_stagnant', False),
                'nse_delta_scope': 'accepted station state minus previous round; upstream may differ'})
            row['code_diagnostic_hypotheses'] = policy_leader_lines(
                None if failed else report.get('diagnosis'), active_keys(phase), False,
                agent.static_input_protocol, agent.prompt_visibility_protocol)
            row['improvement_trend'] = report.get('diagnosis', {}).get('improvement_trend')
            row['rounds_without_improvement'] = report.get('ledger_summary', {}).get('stagnation_count', 0)
            row['stagnation_scope'] = report.get('stagnation_scope', {
                'through_round_id': max(0, round_id - 1),
                'criterion': 'no NSE exceeding historical best + 0.001; not rejected-candidate count'})
            row['historical_best_observed_NSE'] = report.get('best_nse')
        stations[sid] = row
    coordination = getattr(agent, 'context_coordination_result', {}) or {}
    insights = []
    for insight in coordination.get('insights', [])[:5]:
        one = {'description': insight.get('description', ''),
               'recommendation': insight.get('recommendation', ''),
               'severity': insight.get('severity')}
        from .phase_mc_feedback import phase_aware_insight
        insights.append(phase_aware_insight(one, facts['decision_target']))
    state = {'phase_state': facts['phase_state'], 'decision_target': facts['decision_target'],
        'J_total': facts['J_total'], 'outlet_NSE': outlet_nse, 'mean_subbasin_NSE': mean_sub_nse,
        'missing_stations': facts['missing_configured_stations'], 'stations': stations}
    state['global_trend_heuristic'] = coordination.get('global_status')
    if not getattr(agent, 'context_runtime_in_system', False):
        state['configured_bounds'] = agent.ranges
        state['topology'] = {sid: row['topology'] for sid, row in facts['stations'].items()}
    terminal = facts['decision_target'].get('purpose') == 'terminal_review_only'
    task_key = 'refined_terminal_request' if terminal else 'context_leader_request'
    blocks = {'task': agent.prompt_source.get_text(task_key),
        'state': _json(state), 'records': _json(records.body()),
        'cross_station_hypotheses': _json(insights),
        'history': agent.memory.format_full_history_table(),
        'exploration': getattr(agent, 'context_exploration', '') or '',
        'MC_response': getattr(agent, 'context_mc_summary', '') or '',
        'record_legend': agent.prompt_source.get_text('context_record_legend'),
        'configured_station_ids': _json(list(agent.worker_ids))}
    if not reports and diagnostic_text:
        # Direct use outside the graph may provide only the already rendered
        # panel. Keep it rather than silently discard evidence; graph uses raw
        # reports and therefore has no duplicate fallback block.
        blocks['diagnostic_panel_fallback'] = diagnostic_text
    refs = evidence_index({k: v for r in results for k, v in r.get('metrics', {}).items()},
                          feedbacks, ('current_round_selected_candidate', 'accepted_center'))
    return _render(agent, blocks, records, refs)
