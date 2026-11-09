"""Explicit, process-local search ablations over the unchanged shared runtime.

Importing this module installs nothing. The ablation launcher calls configure
before preparing inputs and install before constructing the shared graph. One
process owns one intervention; the returned restoration function is for offline
validation only. Normal MADR entry points never import this module.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from pathlib import Path

SEARCH_ARMS = {
    'C1_no_exploration', 'C2_no_multi_candidate', 'C3_no_phase_schedule',
    'C4_no_mc_no_phases', 'C5_no_anti_flip', 'C6_sadr_no_escape',
    'C7_sadr_no_decay', 'C8_sadr_no_escape_no_decay',
}
_INSTALLED = None


def evaluation_schedule(config, rounds_executed=None, final_confirmation=True):
    """Candidate slots, excluding center-reference and fixed-flux preparation."""
    pipeline = config['phase_config']['pipeline']
    cap = sum(p['max_rounds'] for p in pipeline)
    count = cap if rounds_executed is None else rounds_executed
    if type(count) is not int or count < 1 or count > cap:
        raise ValueError('Requested rounds must be within the configured pipeline')
    slots = config.get('ablation_search', {}).get('global_mc_schedule')
    rows = []
    for phase in pipeline:
        for local in range(1, phase['max_rounds'] + 1):
            rid = len(rows) + 1
            if rid > count:
                return rows
            n = (phase['mc_candidates'] if local <= phase.get('mc_rounds', 0)
                 and phase.get('mc_candidates', 0) else 1)
            if slots is not None:
                n = slots.get(str(rid), 1)
            if rid == 1 or (rid == count and final_confirmation):
                n = 1
            if type(n) is not int or n < 1:
                raise ValueError('Every candidate slot count must be a positive integer')
            rows.append({'round_id': rid, 'phase': phase['name'], 'per_station': n,
                         'candidate_evaluations': n * len(config['workers'])})
    return rows


def candidate_budget(config, rounds_executed=None, final_confirmation=True,
                     exploration_requests=None):
    """Use the shared topology reference slots with the ablation's exact MC slots.

    Exploration information cost and physical reused cost are distinguished by
    callers. By default this function reports physical exploration evaluations.
    """
    from scripts.run_contract import center_reference_budget
    rows = evaluation_schedule(config, rounds_executed, final_confirmation)
    explore = config.get('initial_exploration', {})
    if exploration_requests is None:
        exploration_requests = (len(config['workers']) *
            (int(explore.get('n_near', 0)) + int(explore.get('n_random', 0)))
            if explore.get('enabled') and explore.get('mode', 'fresh') != 'reuse' else 0)
    references, reference_rows = center_reference_budget(config, len(rows), final_confirmation)
    main = sum(row['candidate_evaluations'] for row in rows)
    return {'main_loop': main, 'center_reference_slots': references,
            'center_rechecks': references, 'exploration': exploration_requests,
            'total': main + references + exploration_requests,
            'total_evaluations_upper_bound': main + references + exploration_requests,
            'count_semantics': 'Planned upper bound: authenticated center-reference reuse launches no model. Fixed-flux preparation is separate.',
            'rounds': rows, 'center_rounds': reference_rows}


def _information_budget(config):
    rows = evaluation_schedule(config)
    per_phase = {}
    for row in rows:
        per_phase[row['phase']] = per_phase.get(row['phase'], 0) + row['per_station']
    explore = config.get('initial_exploration', {})
    n = int(explore.get('n_near', 0)) + int(explore.get('n_random', 0)) if explore.get('enabled') else 0
    return {'main_per_station': sum(row['per_station'] for row in rows),
            'exploration_per_station': n,
            'base_per_station': sum(row['per_station'] for row in rows) + n,
            'phase_candidate_evaluations_per_station': per_phase}


def configure(arm, config, budget_mode='matched_reference'):
    """Return an independent team config; never mutate the supplied reference.

    C1 requires preserve_main_schedule: omit exploration without replacing it
    with online rounds. Other arms retain matched_reference or the explicit
    engineering-only smoke_same_rounds mode.
    """
    result = copy.deepcopy(config)
    if arm not in SEARCH_ARMS:
        return result
    permitted = (('preserve_main_schedule',) if arm == 'C1_no_exploration'
                 else ('matched_reference', 'smoke_same_rounds'))
    if budget_mode not in permitted:
        raise ValueError('Arm ' + arm + ' requires budget_mode in ' + repr(permitted))
    if 'ablation_search' in result:
        raise ValueError('Configure a pristine reference, not an already configured ablation')
    pipeline = result['phase_config']['pipeline']
    if any(p.get('patience_rounds') is not None for p in pipeline):
        raise ValueError('Fixed-budget ablations require patience_rounds=null')
    reference = _information_budget(result)
    reference_rows = evaluation_schedule(result)
    meta = {'protocol': 'search-interventions-v1', 'arm': arm,
            'budget_mode': budget_mode, 'reference_budget': reference,
            'reference_pipeline': copy.deepcopy(pipeline)}
    result['ablation_search'] = meta
    if arm == 'C1_no_exploration':
        # Fresh means no inherited source; this arm disables the exploration gate.
        exploration = result.setdefault('initial_exploration', {})
        if (exploration.get('mode', 'fresh') != 'fresh'
                or any(key in exploration for key in ('source', 'seed_from_run',
                    'summary', 'summary_text', 'results', 'bundle', 'cache'))
                or any(key in result for key in ('exploration_summary', 'exploration_results',
                    'context_exploration', '_exploration_summary'))):
            raise ValueError('No-exploration rejects reuse/replay sources and cached summaries')
        exploration.update(enabled=False, mode='fresh')
        meta['main_schedule_policy'] = 'Unchanged reference phases, MC slots, default and final confirmation; no replacement evaluations.'
        meta['omitted_exploration_evaluations_per_station'] = reference['exploration_per_station']
    elif arm == 'C2_no_multi_candidate':
        for phase in pipeline:
            if budget_mode == 'matched_reference':
                phase['max_rounds'] = reference['phase_candidate_evaluations_per_station'][phase['name']]
            phase.update(mc_rounds=0, mc_candidates=0)
    elif arm in ('C3_no_phase_schedule', 'C4_no_mc_no_phases'):
        total = len(reference_rows)
        if arm == 'C4_no_mc_no_phases' and budget_mode == 'matched_reference':
            total = reference['main_per_station']
        result['phase_config']['pipeline'] = [{'name': 'JOINT', 'max_rounds': total,
            'mc_rounds': 0, 'mc_candidates': 0, 'patience_rounds': None}]
        meta['joint_search_from_start'] = True
        meta['global_mc_schedule'] = ({str(row['round_id']): row['per_station']
            for row in reference_rows if row['per_station'] > 1}
            if arm == 'C3_no_phase_schedule' else {})
    elif arm == 'C5_no_anti_flip':
        meta['anti_flip_filter_enabled'] = False
    elif arm.startswith(('C6_', 'C7_', 'C8_')):
        if result.get('method') != 'SADR':
            raise ValueError('SADR interventions require the SADR reference profile')
        meta['escape_enabled'] = arm not in ('C6_sadr_no_escape', 'C8_sadr_no_escape_no_decay')
        meta['decay_enabled'] = arm not in ('C7_sadr_no_decay', 'C8_sadr_no_escape_no_decay')
        # The first round is a default evaluation, not a search proposal.
        meta['fixed_decay_local_round'] = {p['name']: (2 if i == 0 else 1)
                                           for i, p in enumerate(pipeline)}
    result['max_rounds'] = sum(p['max_rounds'] for p in result['phase_config']['pipeline'])
    meta['expected_budget'] = _information_budget(result)
    if budget_mode == 'matched_reference' and meta['expected_budget']['base_per_station'] != reference['base_per_station']:
        raise ValueError('Ablation failed to preserve the reference base evaluation budget')
    return result


_JOINT_PROMPT_REPLACEMENTS = {
    'worker_system_minimal': (
        ('- Phase vic_only: Only adjust soil params. Focus on water balance then peaks.\n- Phase routing_only: Only adjust velocity/diffusivity. Focus on timing.\n- Phase joint: Fine-tune both. Small adjustments only.',
         '- All eight parameters are available from the first search round. Select legal changes from the observed evidence; no parameter-group phases apply.'),
    ),
    'leader_system_minimal': (
        ('Soil (VIC_ONLY):',
         'Soil parameters:'),
        ('Routing (ROUTING_ONLY):',
         'Routing parameters:'),
        ('JOINT: all 8 params, small adjustments only.',
         'JOINT: all eight parameters are available throughout search, without a late-stage small-step restriction.'),
        ('- In VIC_ONLY, fixed default routing may limit outlet NSE; use mean sub-basin NSE as the historical review heuristic, without assuming routing caused the observed error.',
         '- Use mean sub-basin NSE with local and downstream evidence; no parameter group is frozen by a stage schedule.'),
        ('- VIC_ONLY: tune soil params. Use large adjustments early, small later.\n- ROUTING_ONLY: tune velocity/diffusivity only. Check peak_timing_bias.\n- JOINT: fine-tune all params with small adjustments. Phase lengths come from runtime configuration.\n- Phase transitions and stopping follow the runtime schedule; omit their runtime-owned metadata.',
         '- All eight parameters are available from the first search round. The total round budget and final confirmation remain fixed; omit runtime-owned metadata.'),
    ),
    'refined_worker_system': (
        ('VIC_ONLY adjusts soil; ROUTING_ONLY adjusts routing; JOINT uses small changes around the accepted center.',
         'All eight parameters are available from the first search round; choose the size of legal changes from the evidence.'),
        ('within the current scheduled phase; do not change the fixed schedule.',
         'within the legal parameter space; do not change the fixed round budget.'),
    ),
    'refined_leader_system': (
        ('VIC_ONLY adjusts soil parameters; ROUTING_ONLY adjusts velocity/diffusivity; JOINT makes small adjustments around the accepted centers.',
         'All eight parameters are available from the first search round; choose the size of legal changes from the evidence.'),
        ("Preserve frozen values and this experiment's soil_d2 < soil_d3 constraint;",
         "Preserve ownership constraints and this experiment's soil_d2 < soil_d3 constraint;"),
        ('The round schedule is fixed.',
         'The total round budget is fixed.'),
        ('within the scheduled phase. Do not change stopping, phase lengths or inactive radius controls.',
         'within the legal parameter space. Do not change stopping or inactive radius controls.'),
        ('mainly for ROUTING_ONLY or JOINT, but generation timing and upstream arrivals can also explain the bias.',
         'while generation timing and upstream arrivals can also explain the bias.'),
    ),
    'context_shared_contract': (
        ('Anti-flip and phase controls remain active.',
         'Anti-flip and ownership controls remain active; all eight parameters are available throughout search.'),
        ('Fixed scheduling determines stopping and phase changes; do not claim to change them.',
         'The fixed round budget determines stopping; do not claim to change it.'),
    ),
    'leader_phase_joint': (
        ('JOINT: All 8 parameters are available. Always continue.',
         'JOINT: All 8 parameters are available from the first search round; this is not a terminal fine-tuning stage. Always continue.'),
    ),
    'leader_knowledge_minimal': (
        ('- Use this metric primarily in ROUTING_ONLY and JOINT phases',
         '- Use this metric when evaluating routing hypotheses.'),
        ('- Most useful in VIC_ONLY and JOINT phases',
         '- Useful for evaluating soil and storage hypotheses.'),
    ),
}

_ANTI_FLIP_PROMPT_REPLACEMENTS = {
    'worker_system_minimal': (
        ('The legacy anti-flip policy remains active.',
         'Direction reversal filtering is disabled; measured direction records remain factual history.'),
    ),
    'leader_system_minimal': (
        ('The legacy anti-flip policy remains active.',
         'Direction reversal filtering is disabled; measured direction records remain factual history.'),
    ),
    'experiment_worker_plan': (
        ('after the unchanged anti-flip controller and phase locks',
         'after phase locks; direction reversal filtering is disabled'),
    ),
    'context_shared_contract': (
        ('Anti-flip and phase controls remain active.',
         'Direction reversal filtering is disabled. Phase controls remain active.'),
    ),
}


def _replace_prompt_section(text, section, replacements):
    """Each declared source anchor must occur exactly once in its own section."""
    for before, after in replacements.get(section, ()):
        if text.count(before) != 1:
            raise ValueError('Ablation prompt anchor missing or ambiguous: ' + section + ': ' + before[:80])
        text = text.replace(before, after, 1)
    return text


def _validate_prompt_anchors(config, replacements):
    from scripts.prompt_source import PromptSource
    # Read raw sections without marking unused sections as consumed by an agent.
    sections = PromptSource(config.get('static_input_protocol')).sections
    expected_locations = {}
    for section, pairs in replacements.items():
        if section not in sections:
            raise ValueError('Ablation prompt section missing: ' + section)
        _replace_prompt_section(sections[section], section, replacements)
        for before, _ in pairs:
            expected_locations.setdefault(before, set()).add(section)
    for before, expected in expected_locations.items():
        found = {section for section, text in sections.items() if before in text}
        if found != expected:
            raise ValueError('Ablation prompt anchor moved or duplicated across sections: ' + before[:80])


def joint_prompt_text(text, section):
    """Remove staged-search assumptions without changing physical knowledge."""
    return _replace_prompt_section(text, section, _JOINT_PROMPT_REPLACEMENTS)


def anti_flip_prompt_text(text, section):
    return _replace_prompt_section(text, section, _ANTI_FLIP_PROMPT_REPLACEMENTS)



def _audit_global_schedule(original, run_root, config, rounds_executed, accounting,
                           final_confirmation=True):
    """Retain the shared integrity audit, replacing only its MC count projection."""
    result = original(run_root, config, rounds_executed, accounting, final_confirmation)
    scheduled = evaluation_schedule(config, rounds_executed, final_confirmation)
    index = json.loads((Path(run_root) / 'native_audit/candidate_index.json').read_text())
    actual = {row['round_id']: defaultdict(int) for row in scheduled}
    for encoded in index:
        sid, run_id, candidate_id, phase_tag = json.loads(encoded)
        if phase_tag.startswith('round_'):
            rid = int(phase_tag.split('_')[1])
            if rid in actual:
                actual[rid][sid] += 1
    # Shared identity errors do not have this exact count-mismatch shape.
    count_keys = {'round_id', 'expected_per_station', 'actual'}
    mismatches = [row for row in result['round_mismatches'] if set(row) != count_keys]
    stations = {w['subbasin_id'] for w in config['workers']}
    for row in scheduled:
        counts = actual[row['round_id']]
        if set(counts) != stations or any(n != row['per_station'] for n in counts.values()):
            mismatches.append({'round_id': row['round_id'],
                'expected_per_station': row['per_station'], 'actual': dict(counts)})
    expected = result['expected']
    main = sum(row['candidate_evaluations'] for row in scheduled)
    expected.update(rounds=scheduled, main_loop=main,
        total=main + expected['center_rechecks'] + expected['exploration'],
        total_evaluations_upper_bound=main + expected['center_reference_slots'] + expected['exploration'])
    result['round_mismatches'] = mismatches
    result['aligned'] = (accounting.get('candidate_evaluations') == expected['total']
        and not mismatches and not result['center_reference_mismatches'])
    return result


def install(arm, config):
    """Install only this arm's local hooks and return an offline restore callback."""
    global _INSTALLED
    if arm not in SEARCH_ARMS:
        return lambda: None
    if _INSTALLED is not None:
        raise RuntimeError('Search ablations require a fresh process per arm')
    if config.get('ablation_search', {}).get('arm') != arm:
        raise ValueError('Run configure for this arm before installing it')
    _INSTALLED = arm
    undo = []

    def patch(obj, name, replacement):
        undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, replacement)

    def restore():
        global _INSTALLED
        for obj, name, original in reversed(undo):
            setattr(obj, name, original)
        _INSTALLED = None

    try:
        if arm in ('C3_no_phase_schedule', 'C4_no_mc_no_phases'):
            _validate_prompt_anchors(config, _JOINT_PROMPT_REPLACEMENTS)
        elif arm == 'C5_no_anti_flip':
            _validate_prompt_anchors(config, _ANTI_FLIP_PROMPT_REPLACEMENTS)
        from scripts import graph_nodes
        meta = config['ablation_search']
        if arm in ('C3_no_phase_schedule', 'C4_no_mc_no_phases'):
            from scripts import phase_config, run_contract, prompt_source, baseline_control
            slots = copy.deepcopy(meta['global_mc_schedule'])

            def multi(pipeline, phase_idx, local_round):
                rid = sum(p['max_rounds'] for p in pipeline[:phase_idx]) + local_round
                n = slots.get(str(rid), 1)
                return (True, n) if n > 1 else (False, 0)

            patch(phase_config, 'should_use_multi_candidate', multi)
            patch(graph_nodes, 'should_use_multi_candidate', multi)
            original_progress = baseline_control.phase_progress

            def progress(state):
                # The shared plateau counter excludes MC rounds. Keep that
                # behavior for non-contiguous MC windows without changing caps.
                projected = dict(state)
                projected['phase_pipeline'] = copy.deepcopy(state['phase_pipeline'])
                phase = projected['phase_pipeline'][state['current_phase_idx']]
                phase['mc_rounds'] = (state['rounds_in_current_phase']
                    if slots.get(str(state['round_id']), 1) > 1 else 0)
                return original_progress(projected)

            patch(baseline_control, 'phase_progress', progress)
            patch(run_contract, 'expected_candidate_budget', candidate_budget)
            original_audit = run_contract.audit_budget

            def audit(run_root, cfg, rounds_executed, accounting, final_confirmation=True):
                return _audit_global_schedule(original_audit, run_root, cfg, rounds_executed,
                                              accounting, final_confirmation)

            patch(run_contract, 'audit_budget', audit)
            original_text = prompt_source.PromptSource.get_text

            def text(source, section):
                return joint_prompt_text(original_text(source, section), section)

            patch(prompt_source.PromptSource, 'get_text', text)
        elif arm == 'C5_no_anti_flip':
            def no_filter(history, sid, hints, scope):
                return copy.deepcopy(hints), {
                    'protocol': 'anti_flip_disabled_ablation_v1', 'scope': copy.deepcopy(scope),
                    'blocked': [], 'direction_bookkeeping': 'unchanged_measured_direction_momentum_v1'}
            patch(graph_nodes, '_apply_anti_flip', no_filter)
            from scripts import prompt_source
            original_text = prompt_source.PromptSource.get_text

            def text(source, section):
                return anti_flip_prompt_text(original_text(source, section), section)

            patch(prompt_source.PromptSource, 'get_text', text)
        elif arm.startswith(('C6_', 'C7_', 'C8_')):
            from scripts import rule_worker_strategy as rule
            original_configure = rule.RuleWorkerStrategy.configure
            original_update = rule.update_escape_state
            phase_decay = rule.phase_decay

            def configure_rule(strategy, *, phase, local_round, escape_state, guidance):
                selected_escape = escape_state if meta['escape_enabled'] else {}
                original_configure(strategy, phase=phase, local_round=local_round,
                    escape_state=selected_escape, guidance=guidance)
                if not meta['decay_enabled']:
                    strategy.decay = phase_decay(phase, meta['fixed_decay_local_round'][phase])

            patch(rule.RuleWorkerStrategy, 'configure', configure_rule)
            if not meta['escape_enabled']:
                def update(esc, **kwargs):
                    # Keep measured pair validation and receipts; discard state transitions.
                    receipt = original_update(copy.deepcopy(esc), **kwargs)
                    before = copy.deepcopy(esc)
                    esc.update(in_escape=False, tier=0, agent_stagnation=0,
                        round_in_tier=0, found_improvement_in_tier=False)
                    receipt.update(protocol='sadr_escape_disabled_ablation_v1',
                        escape_enabled=False, event='DISABLED_BY_ABLATION',
                        state_before=before, state_after=copy.deepcopy(esc))
                    return receipt
                patch(rule, 'update_escape_state', update)
        return restore
    except Exception:
        restore()
        raise
