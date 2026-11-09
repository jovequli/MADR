"""Local code-diagnostic handoff with Worker LLMs and no Leader calls."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path


def local_guidance(reports, station_ids, active_parameters):
    """Only each station's own top-three code hints enter its handoff."""
    by_station = {r['subbasin_id']: r for r in reports}
    if len(by_station) != len(reports) or set(by_station) != set(station_ids):
        raise ValueError('Local handoff requires exactly one report per station')
    out = []
    for sid in station_ids:
        report = by_station[sid]
        diagnosis = report.get('diagnosis') or {}
        hints = []
        if report.get('simulation_ok') is True:
            for item in diagnosis.get('recommendations', [])[:3]:
                if item.get('param') in active_parameters:
                    hints.append({k: deepcopy(item[k]) for k in
                        ('param', 'direction', 'magnitude') if k in item})
        out.append({'subbasin_id': sid,
            'priority_focus': 'Local code diagnostic hypotheses only; independently assess them against your measured reference and local history.',
            'param_adjustments': hints, 'search_radius_hint': 'keep',
            '_handoff_source': 'local_code_diagnosis', 'trial': None})
    return out


def install(config):
    from scripts import graph_nodes as nodes
    from scripts import context_delivery
    from scripts.prompt_source import PromptSource
    from scripts.leader_agent import LeaderMemory

    def review(state):
        shared = nodes._get_shared(state)
        leader = shared['leader']
        phase_context = nodes.decision_phase_context(state)
        target_phase = nodes.CalibrationPhase(phase_context['decision_target_phase'])
        results = state['round_worker_results']
        # The unchanged record_round performs complete joint-state validation.
        j_total, outlet, mean, consistency = leader.compute_global_score(results)
        decision = {'round_id': state['round_id'], 'stop_or_continue': 'continue',
            'overall_assessment': 'Leader disabled. This is a deterministic station-local diagnostic handoff, not an LLM assessment.',
            'global_score_trend': 'not provided to Workers',
            'next_round_strategy': 'Independent station Worker proposals with local code hints; no cross-station coordinator.',
            'worker_guidance': local_guidance(state.get('diagnostic_reports', []),
                [w['subbasin_id'] for w in state['worker_cfgs']], nodes.get_tunable_keys(target_phase)),
            '_decision_source': 'local_code_handoff'}
        decision = nodes.normalize_decision(decision, [])
        decision = nodes.bind_phase_decision(decision, phase_context)
        memory = LeaderMemory()
        memory.rounds = list(state.get('leader_memory_rounds', []))
        memory.j_history = list(state.get('leader_j_history', []))
        memory.outlet_nse_history = list(state.get('leader_outlet_nse_history', []))
        memory.mean_sub_nse_history = list(state.get('leader_mean_sub_nse_history', []))
        memory.best_global = deepcopy(state.get('leader_best_global') or memory.best_global)
        memory.latest_best_by_subbasin = deepcopy(state.get('leader_latest_best_by_subbasin', {}))
        memory.add_round(state['round_id'], results, j_total, outlet, mean, consistency, decision)
        leader.memory = memory
        from scripts.process_trace import write_round_trace, write_existing_mc_diagnostics
        write_round_trace(state['round_dir'], state['round_id'], state.get('diagnostic_reports', []),
            {}, decision, results, leader.objective_weights, state['current_phase_name'],
            score_values={'J_total': j_total, 'outlet_nse': outlet, 'mean_sub_nse': mean,
                          'consistency_raw': consistency},
            decision_source='local_code_handoff', fallback_reason=None, call_id=None,
            attempt_ids=[], diagnostics_prompt_block='', diagnostic_mode=shared['coordinator'].diagnostic_mode)
        write_existing_mc_diagnostics(state['round_dir'], state['round_id'], shared['workers'],
                                      results, state.get('upstream_map', {}))
        Path(state['round_dir'], 'local_handoff.json').write_text(json.dumps({
            'source': 'local_code_diagnosis', 'leader_llm_calls': 0,
            'cross_station_hypotheses_delivered': False,
            'guidance': decision['worker_guidance']}, indent=2) + '\n')
        return {'leader_decision': decision, 'leader_phase_context': phase_context,
            'leader_memory_rounds': memory.rounds, 'leader_j_history': memory.j_history,
            'leader_outlet_nse_history': memory.outlet_nse_history,
            'leader_mean_sub_nse_history': memory.mean_sub_nse_history,
            'leader_best_global': memory.best_global,
            'leader_latest_best_by_subbasin': memory.latest_best_by_subbasin,
            'leader_tokens': {'prompt': 0, 'completion': 0, 'total': 0}}

    original_plan = context_delivery.compact_worker_plan
    def plan(processed, phase):
        if processed.get('_handoff_source') != 'local_code_diagnosis':
            return original_plan(processed, phase)
        from scripts.refined_worker import refine_guidance_text
        # Share action cleanup and original/effective control identity with E.
        # Only the known local handoff's source label differs from that view.
        shown = json.loads(refine_guidance_text(original_plan(processed, phase)))
        shown.update(source='local_code_diagnostic_hypotheses',
            scope=processed['subbasin_id'],
            interpretation='Station-local hypotheses, not a Leader plan. Choose your own legal values using the current reference and local history.')
        return json.dumps(shown, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

    original_text = PromptSource.get_text
    def text(self, key):
        value = original_text(self, key)
        if key == 'refined_worker_system':
            value = value.replace('Leader', 'local code diagnostic handoff').replace('leader', 'local code diagnostic handoff')
            value += '\nThere is no global Leader in this variant. Local code suggestions are hypotheses, not instructions from an LLM coordinator; independently decide parameters.\n'
        elif key == 'current_evidence_upstream':
            previous = 'Leader guidance was issued after the previous round'
            if value.count(previous) != 1:
                raise ValueError('The upstream evidence source identity anchor changed')
            value = value.replace(previous, 'The local code handoff summarizes the previous round', 1)
        elif key == 'current_evidence_control_note':
            previous = 'the original Leader action'
            if value.count(previous) != 1:
                raise ValueError('The direction-control source identity anchor changed')
            value = value.replace(previous, 'the original local code action', 1)
        return value

    original_record = nodes.record_round
    def record(state):
        result = original_record(state)
        root = Path(state['round_dir'])
        for path in (root / 'messages').glob('*_to_leader.json'):
            data = json.loads(path.read_text())
            data.update(direction='station_to_local_audit', delivered_to_leader=False)
            path.write_text(json.dumps(data, indent=2) + '\n')
        result['comm_totals']['worker_to_leader'] = 0
        note = root / 'leader_decision.md'
        if note.exists():
            note.write_text(note.read_text().replace('Leader Decision', 'Local Code Handoff (Leader Disabled)', 1))
        return result

    nodes.leader_review = review
    nodes.record_round = record
    context_delivery.compact_worker_plan = plan
    PromptSource.get_text = text
