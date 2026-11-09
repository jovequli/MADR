"""Explicit, process-local ablation views of the shared agent input pipeline.

Normal entry points never import this module. Source evidence, diagnostic rules,
acceptance, numerical objectives and executors are not changed. Install exactly
one arm before constructing agents; restarting the process restores the baseline.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

SUPPORTED = ('A2_no_current_upstream_hints', 'B1_mean_nse_feedback',
             'B3_no_expert_knowledge')
_INSTALLED_ARM = None
_ROOT = Path(__file__).resolve().parent
SEMANTICS = {'B1_mean_nse_feedback': 'I2_remove_J_feedback_v2'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Ablation source anchor is missing or ambiguous: ' + old[:70])
    return text.replace(old, new, 1)


def _between(text, start, end, replacement):
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError('Ablation section boundaries are missing or ambiguous')
    left, right = text.index(start), text.index(end)
    if left >= right:
        raise ValueError('Ablation section boundaries are reversed')
    return text[:left] + replacement.rstrip() + '\n\n' + text[right:]


def _knowledge_fragments():
    from scripts.prompt_source import parse_bundle
    return parse_bundle((_ROOT / 'I4_no_expert_knowledge' / 'projection.md').read_text())


def project_static_section(arm, section, text):
    """Apply only the declared static difference; reject changed source anchors."""
    if arm == 'B3_no_expert_knowledge':
        fragments = _knowledge_fragments()
        if section == 'refined_worker_system':
            return _between(text, '## Parameter knowledge', '## Evidence and decisions',
                            fragments['parameter_units'])
        if section == 'refined_leader_system':
            text = _replace_once(text,
                'Historical, revisable priorities: binfilt before Dsmax/Ds, then Ws and depths; water balance before peaks, timing and shape. These are heuristics, not universal sensitivities or a replacement objective. ', '')
            text = _between(text, '## Physical knowledge: three-layer VIC with ARNO baseflow',
                            '## Interpret the supplied diagnostics',
                            fragments['parameter_units'] + '\n\n' + fragments['leader_evidence_limits'])
            text = _between(text, '## Interpret the supplied diagnostics',
                            '## Global diagnosis and next-round guidance', fragments['metric_definitions'])
            text = _replace_once(text,
                'Use the configured graph to distinguish upstream propagation, downstream local response and routing mismatch. Upstream errors are weighted by actual contributing flows and timing, not area alone; they may reinforce or offset. Do not sum nested-station percentage biases as independent contributions. Similar symptoms across stations motivate checking shared explanations, not uniform parameter changes.\n\n',
                'Do not sum nested-station percentage biases as independent contributions.\n\n')
            return text
    if arm == 'B1_mean_nse_feedback':
        text = text.replace(
            'J_total is maximized: a negative current-minus-previous delta is a decrease. ', '')
        text = text.replace('Use the configured global score and station NSE; ',
                            'Use the supplied station metrics; ')
    return text


def without_j_history(memory):
    """Remove only the composite-score column from the shared full-span table.

    Keep every round, all retained station values, missing-detail markers and MC
    records. Do not substitute another score or broaden the station detail window.
    The implementation identity is independent of its public label.
    """
    from scripts.leader_agent import LeaderMemory
    renderer = getattr(LeaderMemory.format_full_history_table, '_ablation_original',
                       LeaderMemory.format_full_history_table)
    text = renderer(memory)
    if text == '(no history yet)':
        return text
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.startswith('R|')), None)
    if header is None or lines[header].split('|')[1] != 'J':
        raise ValueError('J-removal requires the shared Leader history score column')
    projected = []
    for i, line in enumerate(lines):
        if i == header or (i > header and line.split('|')[0].isdigit()):
            fields = line.split('|')
            if len(fields) != len(lines[header].split('|')):
                raise ValueError('Unexpected Leader history width')
            del fields[1]
            line = '|'.join(fields)
        else:
            line = line.replace(
                'J is retained for all rounds; station NSE/PBIAS details are retained only for the recent window. ',
                'Round identities are retained for all rounds; station NSE/PBIAS details are retained only for the recent window. ')
        projected.append(line)
    return '\n'.join(projected)


def project_blocks(arm, agent, blocks):
    """Project current-reference inputs before serialization."""
    shown = deepcopy(blocks)
    worker = hasattr(agent, 'propose_template')
    if arm == 'A2_no_current_upstream_hints' and worker:
        shown.pop('current_upstream_update', None)
        # The current diagnosis, B-reference metrics, full reason, topology and
        # upstream identity checks deliberately remain. This removes only the
        # explicit current upstream change/contrast message, not attribution safety.
        if hasattr(agent, 'current_evidence_projection'):
            agent.current_evidence_projection = deepcopy(agent.current_evidence_projection)
            agent.current_evidence_projection['upstream_update_visible'] = False
            agent.current_evidence_projection['ablation_arm'] = arm
    elif arm == 'B1_mean_nse_feedback' and not worker:
        state = json.loads(shown['state'])
        if 'J_total' not in state:
            raise ValueError('J-removal requires the complete shared Leader state')
        del state['J_total']
        shown['state'] = _json(state)
        shown['history'] = without_j_history(agent.memory)
        # A graph run always provides structured reports; an arbitrary legacy
        # panel has no provenance sufficient to distinguish J from model text.
        if shown.get('diagnostic_panel_fallback'):
            raise ValueError('J-removal requires structured reports, not an untyped diagnostic panel')
    return shown


def _project_runtime_text(text):
    """Drop score formula/weights from the actual injected runtime JSON only."""
    marker = '## Runtime facts\n'
    if text.count(marker) != 1:
        raise ValueError('Runtime facts marker is missing or ambiguous')
    first, rest = text.split(marker, 1)
    facts, end = json.JSONDecoder().raw_decode(rest)
    for key in ('J', 'J_weights', 'penalty'):
        if key not in facts:
            raise ValueError('Missing global-score runtime field: ' + key)
        del facts[key]
    return first + marker + _json(facts) + rest[end:]


def install(arm, config):
    """Install one explicitly selected information intervention in this process."""
    global _INSTALLED_ARM
    if arm not in SUPPORTED:
        raise ValueError('Information arm is not implemented: ' + arm)
    if _INSTALLED_ARM is not None:
        if _INSTALLED_ARM == arm:
            return {'arm': arm, 'scope': 'agent_input_projection', 'already_installed': True,
                    'semantics': SEMANTICS.get(arm, arm)}
        raise RuntimeError('One ablation arm per process; start a new process for another arm')
    if config.get('context_delivery_protocol', {}).get('arm') != 'E_refined':
        raise ValueError('Information ablations require the shared E_refined context')
    from scripts import context_efficiency, prompt_source
    original_project = context_efficiency.project
    def projection(agent, blocks):
        shown, audit = original_project(agent, blocks)
        shown = project_blocks(arm, agent, shown)
        audit = deepcopy(audit)
        audit['information_ablation'] = {'arm': arm, 'scope': 'model_facing_only',
            'semantics': SEMANTICS.get(arm, arm)}
        return shown, audit
    context_efficiency.project = projection

    original_text = prompt_source.PromptSource.get_text
    def get_text(source, section):
        return project_static_section(arm, section, original_text(source, section))
    prompt_source.PromptSource.get_text = get_text
    original_metadata = prompt_source.PromptSource.provenance_metadata
    def metadata(source, **kwargs):
        result = original_metadata(source, **kwargs)
        projected = {name: get_text(source, name) for name in list(source._used)}
        result['ablation_static_projection'] = {
            'arm': arm, 'semantics': SEMANTICS.get(arm, arm), 'base_section_hashes_describe': 'shared unmodified source',
            'effective_section_sha256': {name: hashlib.sha256(text.encode()).hexdigest()
                                         for name, text in sorted(projected.items())},
            'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if arm == 'B3_no_expert_knowledge':
            path = _ROOT / 'I4_no_expert_knowledge' / 'projection.md'
            result['ablation_static_projection']['difference_source'] = str(path)
            result['ablation_static_projection']['difference_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result
    prompt_source.PromptSource.provenance_metadata = metadata

    if arm == 'B1_mean_nse_feedback':
        from scripts import refined_common, leader_agent
        original_runtime = refined_common.refined_runtime_context
        def runtime(c, role):
            text = original_runtime(c, role)
            return _project_runtime_text(text) if role == 'leader' else text
        refined_common.refined_runtime_context = runtime
        original_history = leader_agent.LeaderMemory.format_full_history_table
        def history(memory):
            return without_j_history(memory)
        history._ablation_original = original_history
        leader_agent.LeaderMemory.format_full_history_table = history
        original_fallback = leader_agent.LeaderAgent._fallback_decision
        def fallback(agent, round_id, worker_results, j_total, outlet_nse, mean_sub_nse, phase=None):
            # Compute recovery with exactly the shared arguments and memory.
            # Only the score-derived display text is withheld; actions, phase
            # locks, stopping and the next retry remain the shared implementation.
            decision = original_fallback(agent, round_id, worker_results, j_total,
                                         outlet_nse, mean_sub_nse, phase)
            decision['overall_assessment'] = re.sub(
                r' J_total=[^,]+, trend=[^.]+\.', '', decision['overall_assessment'])
            decision.pop('global_score_trend', None)
            return decision
        leader_agent.LeaderAgent._fallback_decision = fallback
    _INSTALLED_ARM = arm
    return {'arm': arm, 'scope': 'agent_input_projection',
            'semantics': SEMANTICS.get(arm, arm)}
