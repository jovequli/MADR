"""Select one explicit ablation in an isolated process over the shared core."""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from .arm_registry import (implementation_id, arm_label, SENSITIVITY_IMPLEMENTATIONS,
                             VERSION, SUPPORTED_VERSIONS, RELEASE_ALLOWED_IMPLEMENTATIONS,
                             CONTROL_METHODS, method_for_arm, budget_mode_for_arm)

ROOT = Path(__file__).resolve().parents[1]
CONTROLS = CONTROL_METHODS
INFORMATION = {'A2_no_current_upstream_hints', 'B1_mean_nse_feedback', 'B3_no_expert_knowledge'}
SEQUENTIAL = 'A3_strict_sequential'
CODE_ADVICE = 'B2_no_code_diagnostic_hints'
_ACTIVE = None


def protocol(value):
    if not isinstance(value, dict) or set(value) != {'version', 'arm', 'budget_mode'}:
        raise ValueError('Ablation requires version, arm and budget_mode')
    if value['version'] not in SUPPORTED_VERSIONS:
        raise ValueError('Unsupported ablation protocol')
    value = copy.deepcopy(value)
    value['arm'] = implementation_id(value['arm'], value['version'])
    if value['arm'] == 'S3':
        raise ValueError('S3 uses its separate ablations/S3_single_global_agent/run.py entry')
    if value['arm'] not in RELEASE_ALLOWED_IMPLEMENTATIONS:
        raise ValueError('Unknown ablation arm')
    budget_mode = budget_mode_for_arm(value['arm'], value['version'])
    allowed_modes = ((budget_mode, 'smoke_same_rounds') if budget_mode == 'matched_reference'
                     else (budget_mode,))
    if value['budget_mode'] not in allowed_modes:
        raise ValueError('Arm ' + value['arm'] + ' requires budget_mode in ' + repr(allowed_modes))
    return copy.deepcopy(value)


def configure(config, selected):
    selected = protocol(selected)
    arm = selected['arm']
    method = method_for_arm(arm, selected['version'])
    if config.get('method') != method:
        raise ValueError('Selected arm requires method=' + method)
    if 'ablation_protocol' in config:
        raise ValueError('Use a pristine baseline team configuration')
    from .search_interventions import configure as configure_search
    if arm in SENSITIVITY_IMPLEMENTATIONS:
        from .sensitivity_interventions import configure as configure_sensitivity
        result = configure_sensitivity(arm, config, selected['budget_mode'])
    else:
        result = configure_search(arm, config, selected['budget_mode'])
    if arm == SEQUENTIAL:
        from .sequential_intervention import configure as configure_sequential
        result = configure_sequential(result)
    result['ablation_protocol'] = selected
    result['ablation_arm_label'] = arm_label(arm, selected['version'])
    return result


def install(config):
    """Install before graph construction. A normal config is an exact no-op."""
    global _ACTIVE
    if 'ablation_protocol' not in config:
        return
    selected = protocol(config['ablation_protocol'])
    identity = (selected['arm'], selected['budget_mode'])
    if _ACTIVE is not None:
        if _ACTIVE != identity:
            raise RuntimeError('Each process must execute only one ablation')
        return
    base = str(ROOT)
    if base not in sys.path:
        sys.path.insert(0, base)
    arm = selected['arm']
    if arm in SENSITIVITY_IMPLEMENTATIONS:
        from .sensitivity_interventions import install as apply
        apply(arm, config)
    elif arm in INFORMATION:
        from .information_interventions import install as apply
        apply(arm, config)
    elif arm == 'A1_no_leader':
        from .no_leader import install as apply
        apply(config)
    elif arm == CODE_ADVICE:
        from .code_advice_intervention import install as apply
        apply(config)
    elif arm == SEQUENTIAL:
        from .sequential_intervention import install_station
        install_station(config)
    elif arm not in CONTROLS:
        from .search_interventions import install as apply
        apply(arm, config)
    _ACTIVE = identity
