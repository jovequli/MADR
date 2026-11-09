"""Budget and phase sensitivity designs over the shared MADR execution path.

Schedule changes are ordinary shared-runtime configuration. These designs do
not replace the exploration sampler or install shared-function overrides.
"""
from __future__ import annotations

import copy

from .search_interventions import _information_budget, evaluation_schedule

from .arm_registry import (SENSITIVITY_IMPLEMENTATIONS, implementation_id, VERSION)

FRESH_ONLY_ARMS = {'sensitivity_no_exploration_matched'}
_INSTALLED = None

# Actual MC search rounds exclude the first default-parameter evaluation.
DESIGNS = {
    'sensitivity_extended_budget': {'phases': (100, 60, 20), 'mc': (20, 10), 'k': 3},
    'sensitivity_no_exploration_matched': {'phases': (100, 30, 10), 'mc': (10, 5), 'k': 3,
           'exploration_disabled': True},
    'sensitivity_phase40_40_10': {'phases': (40, 40, 10), 'mc': (10, 5), 'k': 3},
    'sensitivity_phase60_20_10': {'phases': (60, 20, 10), 'mc': (10, 5), 'k': 3},
    'sensitivity_phase45_25_20': {'phases': (45, 25, 20), 'mc': (10, 5), 'k': 3},
}


def _require_reference(config):
    if config.get('method') != 'MADR':
        raise ValueError('Sensitivity designs require the MADR reference')
    if any(key in config for key in ('ablation_protocol', 'ablation_search',
                                     'ablation_sensitivity', 'sequential_station_context')):
        raise ValueError('Configure a pristine baseline, not an existing intervention')
    pipeline = config.get('phase_config', {}).get('pipeline', [])
    expected = [('VIC_ONLY', 50, 11, 3), ('ROUTING_ONLY', 30, 5, 3),
                ('JOINT', 10, 0, 0)]
    actual = [(p.get('name'), p.get('max_rounds'), p.get('mc_rounds'),
               p.get('mc_candidates')) for p in pipeline]
    if actual != expected or config.get('max_rounds') != 90:
        raise ValueError('Sensitivity designs must start from the pristine 50/30/10, 90-round reference')
    if any(p.get('patience_rounds') is not None for p in pipeline):
        raise ValueError('Sensitivity designs require fixed budgets and disabled phase patience')
    exploration = config.get('initial_exploration', {})
    if (exploration.get('enabled') is not True or exploration.get('n_near') != 10
            or exploration.get('n_random') != 40):
        raise ValueError('Sensitivity designs must start from the 10-near + 40-global exploration reference')
    if config.get('diagnostic_protocol', {}).get('mode') != 'historical_policy':
        raise ValueError('Sensitivity designs preserve the baseline historical-policy summary and diagnosis')


def configure(arm, config, budget_mode=None):
    """Apply the selected phase schedule and its declared evaluation budget."""
    arm = implementation_id(arm)
    result = copy.deepcopy(config)
    if arm not in SENSITIVITY_IMPLEMENTATIONS:
        return result
    if budget_mode != 'sensitivity_schedule':
        raise ValueError('Sensitivity arms require explicit budget_mode="sensitivity_schedule"')
    _require_reference(config)
    design = copy.deepcopy(DESIGNS[arm])
    explore = result['initial_exploration']
    if arm in FRESH_ONLY_ARMS:
        if explore.get('mode', 'fresh') != 'fresh':
            raise ValueError(arm + ' requires fresh exploration settings; a baseline bundle has a different design')
        source_keys = ('source', 'seed_from_run', 'summary', 'summary_text', 'results',
                       'bundle', 'cache', 'replay_parameter_set_sha256')
        state_keys = ('exploration_summary', 'exploration_results', 'context_exploration',
                      '_exploration_summary')
        if any(key in explore for key in source_keys) or any(key in result for key in state_keys):
            raise ValueError(arm + ' rejects inherited exploration sources or summaries')
    if design.get('exploration_disabled'):
        explore.update(enabled=False, mode='fresh')
    if 'phases' in design:
        for i, phase in enumerate(result['phase_config']['pipeline']):
            phase['max_rounds'] = design['phases'][i]
            actual_mc = design['mc'][i] if i < 2 else 0
            phase['mc_rounds'] = actual_mc + (1 if i == 0 and actual_mc else 0)
            phase['mc_candidates'] = design['k'] if actual_mc else 0
        result['max_rounds'] = sum(design['phases'])
    meta = {'protocol': 'madr-sensitivity-table-a1-v2', 'arm': arm,
            'budget_mode': budget_mode, 'design': design,
            'reference_budget': _information_budget(config),
            'expected_budget': _information_budget(result),
            'normalization': 'Unchanged shared parameter precision, bounds and depth constraint',
            'fresh_exploration_required': False}
    result['ablation_sensitivity'] = meta
    rows = evaluation_schedule(result)
    meta['main_loop_rounds'] = len(rows)
    meta['single_candidate_rounds'] = sum(row['per_station'] == 1 for row in rows)
    meta['multi_candidate_rounds'] = sum(row['per_station'] > 1 for row in rows)
    target_main = 240 if arm == 'sensitivity_extended_budget' else 170 if arm == 'sensitivity_no_exploration_matched' else 120
    if meta['expected_budget']['main_per_station'] != target_main:
        raise ValueError('Configured main evaluation budget disagrees with the sensitivity design')
    return result


def install(arm, config):
    """Validate process ownership; these schedule-only designs need no hooks."""
    global _INSTALLED
    version = config.get('ablation_protocol', {}).get('version', VERSION)
    arm = implementation_id(arm, version)
    if arm not in SENSITIVITY_IMPLEMENTATIONS:
        return lambda: None
    if _INSTALLED is not None:
        raise RuntimeError('Sensitivity arms require a fresh process per arm')
    meta = config.get('ablation_sensitivity', {})
    if implementation_id(meta.get('arm', ''), version) != arm or meta.get('budget_mode') != 'sensitivity_schedule':
        raise ValueError('Configure the sensitivity arm before installing it')
    _INSTALLED = arm

    def restore():
        global _INSTALLED
        _INSTALLED = None

    return restore
