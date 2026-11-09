"""Hide code-authored hydrological advice from agent inputs, not from execution.

The independent ablation launcher installs this module before agent construction.
Raw diagnostic calculations, candidate recovery and all numerical protections stay
in the shared core. Private source tags distinguish fallback advice from actual
LLM wording; no content-based deletion is applied to model-authored reasoning.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

ARM = 'B2_no_code_diagnostic_hints'
_SOURCE = '_ablation_code_advice_source'
_FALLBACK_BRANCHES = {'historical_rule_fallback', 'neutral_fallback',
                      'graph_neutral_fallback', 'graph_center_fallback'}
_INSTALLED = False
SEMANTICS = 'I3_online_code_advice_only_v2'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _static_projection(section, text):
    """Keep physical knowledge; remove only references to now-withheld advice."""
    replacements = {
        'refined_worker_system': [('supplied previous diagnosis', 'supplied measured evidence')],
        'current_evidence_system_old_phrase': [('supplied previous diagnosis', 'supplied measured evidence')],
        'current_evidence_system_phrase': [('diagnosis bound to the current measured proposal reference',
                                           'evidence bound to the current measured proposal reference')],
        'refined_leader_system': [
            ('Review the supplied previous diagnosis and observed response, then retain or revise the hypothesis.',
             'Review the supplied measured history and observed response, then retain or revise the hypothesis.'),
            ('Code suggestions may be accepted, rejected or revised.',
             'Use the measurements and supplied professional knowledge to form testable parameter hypotheses.')],
    }
    for old, new in replacements.get(section, []):
        if text.count(old) != 1:
            raise ValueError('Code-advice ablation static anchor changed: ' + section)
        text = text.replace(old, new, 1)
    return text


def tag_recovery_decision(decision):
    """Annotate a known code-owned return value without changing its actions."""
    out = deepcopy(decision)
    source = out.get('_decision_source')
    if source not in {'historical_rule_fallback', 'neutral_fallback'}:
        raise ValueError('Unexpected source from shared fallback decision')
    for guidance in out.get('worker_guidance', []):
        guidance[_SOURCE] = source
    return out


def code_source(guidance):
    if not isinstance(guidance, dict):
        return None
    source = guidance.get(_SOURCE)
    if source is None:
        source = (guidance.get('_leader_original') or {}).get(_SOURCE)
    if source is not None and source not in {'historical_rule_fallback', 'neutral_fallback'}:
        raise ValueError('Unknown code-advice source tag')
    return source


def recovery_plan(guidance, phase):
    """A factual availability notice, never code-suggested future parameter moves."""
    shown = {'phase': phase, 'effective_actions': [],
             'priority_focus': 'Leader response unavailable; no LLM guidance supplied.',
             'guidance_source': code_source(guidance)}
    if guidance.get('_guidance_identity'):
        shown['guidance_target'] = deepcopy(guidance['_guidance_identity'])
    return 'Effective Leader guidance:\n' + _json(shown)


def project_feedback(feedback, rendered):
    """Strip only source-proven code hypotheses; retain executions and real LLM text."""
    if not feedback or not isinstance(rendered, dict):
        return rendered
    out = deepcopy(rendered)
    source = code_source(feedback.get('leader_original_plan'))
    if source:
        # These changes describe filtering of a recovery suggestion, not a
        # model-authored hypothesis. Actual candidate/reference P/M/E remain.
        out.pop('control_changes', None)
        out['leader_guidance_source'] = source
    branch = feedback.get('branch')
    if branch in _FALLBACK_BRANCHES:
        out['candidate_proposal_reasoning'] = 'Code recovery candidate; no successful Worker LLM proposal.'
        out['candidate_source'] = branch
        out.pop('trial', None)
        out.pop('trial_reference_scope', None)
    return out


def _worker_feedback_source(agent, shown):
    """The shared compact Worker view omits source fields; restore factual origins."""
    history = getattr(getattr(agent, 'memory', None), 'history', [])
    feedback = (history[-1].get('experiment_feedback') or {}) if history else {}
    branch = feedback.get('branch')
    source = code_source(feedback.get('leader_original_plan'))
    if not (source or branch in _FALLBACK_BRANCHES):
        return
    raw = shown.get('selected_candidate_feedback')
    if not raw:
        return
    value = json.loads(raw)
    if not isinstance(value, dict):
        return
    if source:
        value['leader_guidance_source'] = source
    if branch in _FALLBACK_BRANCHES:
        value['candidate_source'] = branch
    shown['selected_candidate_feedback'] = _json(value)


def project_blocks(agent, blocks):
    """Apply after refinement/current evidence, keeping metric records untouched."""
    shown = deepcopy(blocks)
    worker = hasattr(agent, 'propose_template')
    removed = []
    if worker:
        for key in ('previous_diagnosis', 'current_reference_diagnosis'):
            if key in shown:
                removed.append(key); shown.pop(key)
        _worker_feedback_source(agent, shown)
        if hasattr(agent, 'current_evidence_projection'):
            agent.current_evidence_projection = deepcopy(agent.current_evidence_projection)
            agent.current_evidence_projection.update(ablation_arm=ARM,
                diagnosis_visible=False, reason_visible_characters=0,
                diagnosis_visibility='withheld_code_advice; current measured reference retained')
    else:
        state = json.loads(shown['state'])
        if 'global_trend_heuristic' in state:
            removed.append('state.global_trend_heuristic')
            state.pop('global_trend_heuristic')
        for sid, row in state.get('stations', {}).items():
            for key in ('code_diagnostic_hypotheses',):
                if key in row:
                    removed.append('state.stations.' + sid + '.' + key); row.pop(key)
            summary = row.get('round_summary')
            if isinstance(summary, dict):
                for key in ('timing_status', 'recession_status'):
                    if key in summary:
                        removed.append('state.stations.' + sid + '.round_summary.' + key)
                        summary.pop(key)
        shown['state'] = _json(state)
        if 'cross_station_hypotheses' in shown:
            removed.append('cross_station_hypotheses'); shown.pop('cross_station_hypotheses')
        if shown.get('diagnostic_panel_fallback'):
            # Without structured reports, arbitrary text cannot be safely split
            # into true observations versus advice. The graph supplies reports.
            raise ValueError('Code-advice ablation requires structured reports, not an untyped diagnostic panel')
    return shown, removed


def install(config):
    """Install only in an explicitly selected, fresh B2 process."""
    global _INSTALLED
    if _INSTALLED:
        return {'arm': ARM, 'already_installed': True, 'semantics': SEMANTICS}
    if config.get('method', 'MADR') != 'MADR':
        raise ValueError('Code-advice input ablation requires MADR')
    if config.get('context_delivery_protocol', {}).get('arm') != 'E_refined':
        raise ValueError('Code-advice input ablation requires E_refined')
    from scripts import (context_delivery, context_efficiency,
                         leader_agent, prompt_source)

    original_fallback = leader_agent.LeaderAgent._fallback_decision
    def fallback(agent, *args, **kwargs):
        return tag_recovery_decision(original_fallback(agent, *args, **kwargs))
    leader_agent.LeaderAgent._fallback_decision = fallback

    original_plan = context_delivery.compact_worker_plan
    def plan(guidance, phase):
        return recovery_plan(guidance, phase) if code_source(guidance) else original_plan(guidance, phase)
    context_delivery.compact_worker_plan = plan

    original_feedback = context_delivery.compact_feedback
    def feedback(value, records, include_reasoning=False):
        rendered = original_feedback(value, records, include_reasoning=include_reasoning)
        return project_feedback(value, rendered)
    context_delivery.compact_feedback = feedback

    # Initial exploration is a separate information source. Its original
    # numerical summary and association hypotheses reach both roles unchanged.

    original_text = prompt_source.PromptSource.get_text
    def get_text(source, section):
        return _static_projection(section, original_text(source, section))
    prompt_source.PromptSource.get_text = get_text
    original_metadata = prompt_source.PromptSource.provenance_metadata
    def metadata(source, **kwargs):
        result = original_metadata(source, **kwargs)
        projected = {name: get_text(source, name) for name in list(source._used)}
        result['code_advice_ablation'] = {'arm': ARM, 'semantics': SEMANTICS,
            'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'effective_section_sha256': {name: _hash(text) for name, text in sorted(projected.items())},
            'base_section_hashes_describe': 'shared unmodified source',
            'exploration_projection': 'unchanged shared exploration summary, including association hypotheses'}
        return result
    prompt_source.PromptSource.provenance_metadata = metadata

    original_projection = context_efficiency.project
    def projection(agent, blocks):
        shown, audit = original_projection(agent, blocks)
        shown, removed = project_blocks(agent, shown)
        audit = deepcopy(audit)
        audit['code_advice_ablation'] = {'arm': ARM, 'semantics': SEMANTICS, 'removed_fields': removed,
            'preserved': 'complete exploration summary, measured values, execution identities, LLM wording, expert knowledge and safeguards'}
        return shown, audit
    context_efficiency.project = projection
    _INSTALLED = True
    return {'arm': ARM, 'scope': 'source_aware_agent_input_projection', 'semantics': SEMANTICS}
