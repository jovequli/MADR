"""Resolve experiment arm labels to fixed intervention identities."""

VERSION = 'madr-ablation-v1'
SUPPORTED_VERSIONS = frozenset({VERSION})
ARM_TO_IMPLEMENTATION = {
    'S1': 'A1_no_leader',
    'S2': 'A3_strict_sequential',
    'I1': 'A2_no_current_upstream_hints',
    'I2': 'B1_mean_nse_feedback',
    'I3': 'B2_no_code_diagnostic_hints',
    'I4': 'B3_no_expert_knowledge',
    'I5': 'C5_no_anti_flip',
    'P1': 'C1_no_exploration',
    'P2': 'C2_no_multi_candidate',
    'P3': 'C3_no_phase_schedule',
    'P4': 'C4_no_mc_no_phases',
    'SADR-E': 'C6_sadr_no_escape',
    'SADR-D': 'C7_sadr_no_decay',
    'SADR-ED': 'C8_sadr_no_escape_no_decay',
}
SENSITIVITY_TO_IMPLEMENTATION = {
    'H1': 'sensitivity_extended_budget',
    'H2': 'sensitivity_no_exploration_matched',
    'H3': 'sensitivity_phase40_40_10',
    'H4': 'sensitivity_phase60_20_10',
    'H5': 'sensitivity_phase45_25_20',
}
SENSITIVITY_ARMS = frozenset(SENSITIVITY_TO_IMPLEMENTATION)
SENSITIVITY_IMPLEMENTATIONS = frozenset(SENSITIVITY_TO_IMPLEMENTATION.values())
CONTROL_METHODS = {'control_madr': 'MADR', 'control_sadr': 'SADR'}
EXTERNAL_ARMS = frozenset({'S3'})
SADR_IMPLEMENTATIONS = frozenset({
    'C6_sadr_no_escape', 'C7_sadr_no_decay', 'C8_sadr_no_escape_no_decay',
})
RELEASE_ALLOWED_IMPLEMENTATIONS = frozenset({
    *ARM_TO_IMPLEMENTATION.values(), *SENSITIVITY_IMPLEMENTATIONS, *CONTROL_METHODS,
})
NO_EXPLORATION = frozenset({'C1_no_exploration', 'sensitivity_no_exploration_matched'})
FRESH_EXPLORATION_ONLY = frozenset({'A3_strict_sequential', *NO_EXPLORATION})
_INFORMATION_SCOPES = {
    'B1_mean_nse_feedback': 'remove_J_feedback_without_replacement',
    'B2_no_code_diagnostic_hints': 'remove_main_loop_code_advice_keep_full_exploration',
}


def implementation_id(arm, version=VERSION):
    """Resolve a versioned label; retained implementation identities are idempotent."""
    if version not in SUPPORTED_VERSIONS:
        raise ValueError('Unsupported ablation protocol: ' + str(version))
    if not isinstance(arm, str):
        raise ValueError('Ablation arm must be a string')
    semantic = SENSITIVITY_TO_IMPLEMENTATION.get(arm, ARM_TO_IMPLEMENTATION.get(arm, arm))
    if semantic not in RELEASE_ALLOWED_IMPLEMENTATIONS | EXTERNAL_ARMS:
        raise ValueError('Unknown ablation arm for ' + version + ': ' + arm)
    return semantic


def arm_label(arm, version=VERSION):
    semantic = implementation_id(arm, version)
    reverse = {value: key for key, value in
               {**ARM_TO_IMPLEMENTATION, **SENSITIVITY_TO_IMPLEMENTATION}.items()}
    return reverse.get(semantic, semantic)


def method_for_arm(arm, version=VERSION):
    semantic = implementation_id(arm, version)
    if semantic in EXTERNAL_ARMS:
        raise ValueError('S3 uses its separate external baseline entry')
    return CONTROL_METHODS.get(semantic, 'SADR' if semantic in SADR_IMPLEMENTATIONS else 'MADR')


def budget_mode_for_arm(arm, version=VERSION):
    semantic = implementation_id(arm, version)
    if semantic in EXTERNAL_ARMS:
        raise ValueError('S3 uses its separate external baseline entry')
    if semantic in SENSITIVITY_IMPLEMENTATIONS:
        return 'sensitivity_schedule'
    return 'preserve_main_schedule' if semantic == 'C1_no_exploration' else 'matched_reference'


def information_scope_for_arm(arm, version=VERSION):
    return _INFORMATION_SCOPES.get(implementation_id(arm, version))
