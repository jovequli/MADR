"""Local agent views for a frozen-upstream sequential station process.

The executor adapter supplies one active station and immutable ancestors. This
module changes only task scope and displayed feedback; local physical knowledge,
diagnostic advice, output schemas, numerical evidence and protections stay shared.
"""
from __future__ import annotations

from copy import copy, deepcopy
import hashlib
import json
from pathlib import Path

ARM = 'A3_strict_sequential'
_CONTEXT_KEY = 'sequential_station_context'
_INSTALLED_STATION = None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Sequential prompt anchor changed: ' + old[:80])
    return text.replace(old, new, 1)


def project_static(section, text):
    replacements = {
        'refined_leader_system': [
            ('Coordinate VIC+routing calibration across the nested subbasins. Identify network bottlenecks, reconcile local and downstream evidence, and guide the target round. Use the configured global score and station NSE; distinguish local gains from network gains.',
             'Guide VIC+routing calibration for the active station in this sequential task. Diagnose its measured response and guide the target round to maximize its evaluation-period NSE. Previously calibrated upstream parameters are fixed inputs; do not propose changes to them or infer unreported downstream performance.'),
            ('## Global diagnosis and next-round guidance', '## Local diagnosis and next-round guidance'),
            ('Use the configured graph to distinguish upstream propagation, downstream local response and routing mismatch. Upstream errors are weighted by actual contributing flows and timing, not area alone; they may reinforce or offset. Do not sum nested-station percentage biases as independent contributions. Similar symptoms across stations motivate checking shared explanations, not uniform parameter changes.',
             'Use the active station\'s observations and fixed upstream physical background to assess local runoff generation and routing hypotheses. Upstream errors may affect its hydrograph, but upstream parameters cannot be changed during this station\'s calibration. Do not infer other stations\' performance or sum nested-station percentage biases as independent contributions.'),
            ('Give a short network assessment and station actions supported by evidence.',
             'Give a short local assessment and active-station actions supported by evidence.')],
        'refined_leader_output': [
            ('Cover each configured station exactly once. Give a concise global bottleneck assessment and actionable station guidance;',
             'Cover the active configured station exactly once. Give a concise local assessment and actionable guidance only for that station;')],
        'context_leader_request': [
            ('Review the coherent accepted joint state and each station\'s selected-candidate feedback below. Provide next-round guidance for every configured station; retain or revise hypotheses according to evidence.',
             'Review the active station\'s accepted parameter vector and selected-candidate feedback below. Provide next-round guidance only for that station; retain or revise hypotheses according to its evidence. Upstream parameter inputs remain frozen.')],
        'refined_terminal_request': [
            ('Review the final confirmed joint result. No next round exists; cover each station with empty param_adjustments. Give a brief final assessment under the same JSON contract.',
             'Review the active station\'s final confirmed parameter vector. No next local round exists; cover that station with empty param_adjustments. Give a brief local assessment under the same JSON contract.')],
    }
    for old, new in replacements.get(section, []):
        text = _replace_once(text, old, new)
    return text


def checked_context(config):
    context = config.get(_CONTEXT_KEY)
    if not isinstance(context, dict) or context.get('protocol') != 'strict-sequential-station-v1':
        raise ValueError('Sequential agent prompts require an explicit station context')
    sid = context.get('active_station')
    if [worker.get('subbasin_id') for worker in config.get('workers', [])] != [sid]:
        raise ValueError('Sequential prompts require exactly one active configured station')
    if config.get('method') != 'MADR' or config.get('context_delivery_protocol', {}).get('arm') != 'E_refined':
        raise ValueError('Sequential prompts require the shared MADR E_refined pipeline')
    if config.get('objective_weights') != {'mean_nse': 1.0, 'consistency': 0.0}:
        raise ValueError('Sequential station compatibility score must be local NSE')
    original = {w['subbasin_id']: w for w in context.get('original_workers', [])}
    upstream_map = context.get('original_upstream_map', {})
    if sid not in original or sid not in upstream_map:
        raise ValueError('Sequential station is absent from original topology')
    ancestors = set()
    pending = list(upstream_map[sid])
    while pending:
        parent = pending.pop()
        if parent == sid or parent not in original or parent not in upstream_map:
            raise ValueError('Invalid sequential upstream topology')
        if parent not in ancestors:
            ancestors.add(parent)
            pending.extend(upstream_map[parent])
    frozen = context.get('frozen_ancestors', {})
    if not isinstance(frozen, dict) or set(frozen) != ancestors:
        raise ValueError('Frozen upstream context must include exactly the active station ancestors')
    for ancestor, record in frozen.items():
        if not isinstance(record.get('effective_parameters'), dict):
            raise ValueError('Frozen upstream effective parameters are missing: ' + ancestor)
    return deepcopy(context)


def local_topology(context):
    sid = context['active_station']
    original = {w['subbasin_id']: w for w in context['original_workers']}
    upstream = list(context['original_upstream_map'][sid])
    return {sid: {'upstream': upstream,
                  'downstream': list(original[sid].get('neighbors_downstream', [])),
                  'has_configured_upstream': bool(upstream),
                  'upstream_parameters_frozen': True,
                  'only_active_station_observed_and_adjustable': True}}


def fixed_background(context):
    """No ancestor scores, run paths or diagnoses enter this physical input view."""
    return {'active_station': context['active_station'],
            'scope': 'Optimize only the active station; all listed upstream parameters remain fixed.',
            'topology': local_topology(context),
            'frozen_upstream_parameters': {
                sid: deepcopy(value['effective_parameters'])
                for sid, value in context['frozen_ancestors'].items()}}


def project_runtime(text, role, context):
    marker = '## Runtime facts\n'
    if text.count(marker) != 1:
        raise ValueError('Sequential runtime facts marker changed')
    first, remainder = text.split(marker, 1)
    facts, end = json.JSONDecoder().raw_decode(remainder)
    if role == 'leader':
        for key in ('downstream', 'J_weights', 'J', 'penalty', 'representative_round'):
            if key not in facts:
                raise ValueError('Sequential runtime field changed: ' + key)
            facts.pop(key)
        facts['objective'] = 'Maximize active-station evaluation-period NSE.'
        facts['representative_round'] = 'Highest local NSE among valid complete parameter vectors of the active station.'
    facts['sequential_station'] = fixed_background(context)
    return first + marker + _json(facts) + remainder[end:]


def local_history(memory, renderer):
    if len(memory.j_history) != len(memory.mean_sub_nse_history):
        raise ValueError('Sequential local NSE history is incomplete')
    view = copy(memory)
    view.j_history = list(memory.mean_sub_nse_history)
    text = renderer(view)
    return text.replace('J is retained for all rounds', 'Local NSE is retained for all rounds').replace('R|J|', 'R|local_NSE|')


def project_blocks(agent, blocks, context, history_renderer):
    shown = deepcopy(blocks)
    state = json.loads(shown['state'])
    sid = context['active_station']
    if hasattr(agent, 'propose_template'):
        if state.get('station') != sid:
            raise ValueError('Sequential Worker request is not for the active station')
        state['topology'] = local_topology(context)[sid]
        payload = getattr(agent, 'current_evidence', {}) or {}
        if payload.get('upstream_changes') or payload.get('background', {}).get('upstream_changed'):
            raise ValueError('Frozen upstream parameters changed within sequential station calibration')
        # No upstream-change narrative is meaningful in an immutable context.
        shown.pop('current_upstream_update', None)
    else:
        if set(state.get('stations', {})) != {sid}:
            raise ValueError('Sequential Leader received another station\'s observations')
        from scripts.factual_context import objective_facts
        original = state.pop('J_total')
        score = state.pop('mean_subbasin_NSE')
        state.pop('outlet_NSE', None)
        state.pop('global_trend_heuristic', None)
        history = [dict(row, j_total=row.get('mean_sub_nse')) for row in agent.memory.rounds]
        state['local_NSE_feedback'] = objective_facts(original['current_round_id'], score, history)
        state['active_station'] = sid
        if 'topology' in state:
            state['topology'] = local_topology(context)
        summary = state['stations'][sid].get('round_summary')
        if isinstance(summary, dict) and 'nse_delta_scope' in summary:
            summary['nse_delta_scope'] = 'accepted active-station state minus previous local round; upstream inputs fixed'
        scopes = state.get('shared_station_field_meanings', {})
        if 'round_summary.nse_delta_scope' in scopes:
            scopes['round_summary.nse_delta_scope'] = 'accepted active-station state minus previous local round; upstream inputs fixed'
        shown.pop('cross_station_hypotheses', None)
        if shown.get('diagnostic_panel_fallback'):
            raise ValueError('Sequential prompts require structured local diagnostic reports')
        shown['history'] = local_history(agent.memory, history_renderer)
    shown['state'] = _json(state)
    return shown


def install(config):
    """Install once before construction in the isolated active-station child."""
    global _INSTALLED_STATION
    context = checked_context(config)
    sid = context['active_station']
    if _INSTALLED_STATION is not None:
        if _INSTALLED_STATION != sid:
            raise RuntimeError('Each sequential station requires a fresh process')
        return {'arm': ARM, 'station': sid, 'already_installed': True}
    from scripts import context_efficiency, factual_context, prompt_source, refined_common, leader_agent
    original_topology = factual_context.configured_topology
    def topology(c):
        if c.get(_CONTEXT_KEY):
            active = checked_context(c)
            return local_topology(active)
        return original_topology(c)
    factual_context.configured_topology = topology

    original_text = prompt_source.PromptSource.get_text
    def get_text(source, section):
        return project_static(section, original_text(source, section))
    prompt_source.PromptSource.get_text = get_text
    original_metadata = prompt_source.PromptSource.provenance_metadata
    def metadata(source, **kwargs):
        result = original_metadata(source, **kwargs)
        result['sequential_station_projection'] = {
            'arm': ARM, 'active_station': sid,
            'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'base_section_hashes_describe': 'shared unmodified source',
            'effective_section_sha256': {name: hashlib.sha256(get_text(source, name).encode()).hexdigest()
                for name in sorted(source._used)},
            'fixed_background_sha256': hashlib.sha256(_json(fixed_background(context)).encode()).hexdigest()}
        return result
    prompt_source.PromptSource.provenance_metadata = metadata

    original_runtime = refined_common.refined_runtime_context
    def runtime(c, role):
        return project_runtime(original_runtime(c, role), role, context)
    refined_common.refined_runtime_context = runtime
    original_history = leader_agent.LeaderMemory.format_full_history_table
    def history(memory):
        return local_history(memory, original_history)
    leader_agent.LeaderMemory.format_full_history_table = history

    original_fallback = leader_agent.LeaderAgent._fallback_decision
    def fallback(agent, *args, **kwargs):
        # This return value is known to be authored by the shared recovery code.
        # Only its score label changes; legal actions and recovery are untouched.
        decision = deepcopy(original_fallback(agent, *args, **kwargs))
        if decision.get('_decision_source') not in ('historical_rule_fallback', 'neutral_fallback'):
            raise ValueError('Unexpected sequential recovery decision source')
        decision['overall_assessment'] = _replace_once(
            decision['overall_assessment'], 'J_total=', 'local_NSE=')
        return decision
    leader_agent.LeaderAgent._fallback_decision = fallback

    original_projection = context_efficiency.project
    def projection(agent, blocks):
        shown, audit = original_projection(agent, blocks)
        shown = project_blocks(agent, shown, context, original_history)
        audit = deepcopy(audit)
        audit['sequential_station_projection'] = {'arm': ARM, 'active_station': sid,
            'scope': 'local observations and immutable upstream physical parameters; no global score or coordination'}
        return shown, audit
    context_efficiency.project = projection
    _INSTALLED_STATION = sid
    return {'arm': ARM, 'station': sid, 'scope': 'local_station_prompt_projection'}
