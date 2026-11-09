"""Lossless numerical records and role-specific runtime facts for agent inputs.

Only the online view changes. Raw source objects, validation, execution and
provider accounting are outside this module and remain authoritative.
"""
from __future__ import annotations
import copy
import json
from .factual_context import METRICS


def refine_records(body):
    """Keep exact P/M values and E identities; omit common audit-only metadata.

    Dates are a shared runtime fact, while missing-signature status remains at M.
    Unknown evaluations never merge, even if their numerical values coincide.
    Full upstream/native identity maps stay in the existing process trace.
    """
    result = {'parameters': copy.deepcopy(body.get('parameters', {})),
              'measurements': {}, 'evaluations': {}}
    for key, measured in body.get('measurements', {}).items():
        result['measurements'][key] = {
            field: copy.deepcopy(value) for field, value in measured.items()
            if field in METRICS or field in ('diagnostic_status', 'n_scored_days')
        }
    aliases = {'params': 'p', 'metrics': 'm', 'upstream': 'u',
               'evaluation_ok': 'ok', 'round_id': 'r',
               'evaluation_round_id': 'eval', 'observed_round_id': 'observed',
               'candidate_id': 'candidate', 'source': 'source',
               'accepted_record_source': 'accepted_source',
               'accepted_as_center_current_round': 'accepted_now',
               'phase': 'phase', 'execution': 'exec',
               'result_scope': 'scope'}
    for key, evaluation in body.get('evaluations', {}).items():
        result['evaluations'][key] = {
            aliases[field]: copy.deepcopy(value) for field, value in evaluation.items()
            if field in aliases
        }
    return result


def refined_runtime_context(config, role):
    """Project frozen numerical facts, never alter or round their values."""
    from .execution_protocol import runtime_context, DECIMALS
    from .prompt_source import render_static
    runtime_context(config)  # Retain all original period/bound/topology gates.
    if role not in ('leader', 'worker'):
        raise ValueError('Unknown refined role')
    facts = {
        'bounds': config['param_ranges'], 'decimals': DECIMALS,
        'simulate': config['simulation_period'],
        'warmup': config['warmup_period'], 'evaluate': config['evaluation_period'],
    }
    if role == 'leader':
        facts['downstream'] = {w['subbasin_id']: w.get('neighbors_downstream', [])
                               for w in config['workers']}
        weights = config.get('objective_weights', {})
        facts['J_weights'] = {'mean_nse': weights.get('mean_nse', .90),
                              'consistency': weights.get('consistency', .10)}
        facts['J'] = 'mean_nse_weight * mean(station NSE) - consistency_weight * penalty'
        facts['penalty'] = 'mean of abs(peak_ratio-1) and abs(lowflow_ratio-1) over all stations'
        facts['representative_round'] = 'highest mean station NSE among complete accepted joint vectors'
    return '\n\n' + render_static('refined_runtime_facts',
        protocol=config.get('static_input_protocol'),
        facts_json=json.dumps(facts, ensure_ascii=False, separators=(',', ':')))


def refined_output_contract(role, prompt_source):
    """Expose a schema-derived executable subset; optional annotations stay valid.

    The local schema is not weakened or replaced. This view does not solicit a
    second structured reasoning report; actual replies are still archived whole.
    """
    from .output_contract import load_output_schema, _shape
    schema = load_output_schema(role)
    if role == 'worker':
        keep = {'vic', 'routing', 'reasoning'}
    elif role == 'leader':
        keep = {'overall_assessment', 'worker_guidance'}
    else:
        raise ValueError('Unknown refined role')
    view = copy.deepcopy(schema)
    view['properties'] = {key: value for key, value in view['properties'].items() if key in keep}
    if role == 'leader':
        guidance = view['properties']['worker_guidance']['items']
        guidance['properties'] = {key: value for key, value in guidance['properties'].items()
                                  if key in {'subbasin_id', 'priority_focus', 'param_adjustments'}}
        action = guidance['properties']['param_adjustments']['items']
        action['properties'] = {key: value for key, value in action['properties'].items()
                                if key in {'param', 'direction', 'magnitude', 'reasoning'}}
    return prompt_source.get_text('refined_' + role + '_output').replace('{shape}', _shape(view))
