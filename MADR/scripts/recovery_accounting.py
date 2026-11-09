"""Additive failure-recovery accounting, independent of calibration decisions."""
from collections import Counter
import json
from pathlib import Path

_RECOVERY_BRANCHES = {
    'historical_rule_fallback', 'neutral_fallback',
    'graph_neutral_fallback', 'graph_center_fallback',
}


def record_worker_recovery_slot(owner, *, round_id, phase, candidate_id, branch,
                                proposal=None, slot_recovery=None, reason=None, ok=None):
    """Record one evaluated replacement slot, never a normal SADR rule proposal."""
    if branch not in _RECOVERY_BRANCHES or hasattr(owner, 'rule_strategy'):
        return None
    from .process_trace import append_event, note_trace_error
    path = None
    try:
        output_root = getattr(owner, '_failure_output_root', None) or getattr(owner, '_trace_output_dir', None)
        if not output_root:
            raise ValueError('Recovery owner has no bound output directory')
        path = Path(output_root) / 'llm_recoveries.jsonl'
        proposal = proposal if isinstance(proposal, dict) else {}
        recovery = proposal.get('recovery') or {}
        row = {
            'status': 'worker_code_candidate_evaluated', 'recovery_version': 2,
            'role': 'Worker', 'agent_id': getattr(owner, 'agent_id', None),
            'subbasin_id': owner.subbasin_id, 'round_id': round_id, 'phase': phase,
            'candidate_id': candidate_id, 'branch': branch, 'fallback_source': branch,
            'reason': reason or proposal.get('fallback_reason'),
            'call_id': proposal.get('call_id'), 'attempt_ids': proposal.get('attempt_ids', []),
            'failure_class': proposal.get('failure_class') or recovery.get('failure_class'),
            'slot_recovery': slot_recovery, 'model_evaluation_ok': ok,
            'recovery_origin': 'graph_mc_outer' if slot_recovery else 'worker_agent',
            'recovery_scope': 'evaluated_worker_candidate_slot',
        }
        append_event(path, row)
        return row
    except Exception as error:
        note_trace_error(path or Path.cwd() / 'unbound_worker_recovery', 'worker_recovery_accounting', error)
        return None


def _rows(path):
    if path.is_file():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def _logical_key(row):
    role = str(row.get('role', '')).lower()
    if row.get('call_id'):
        return role, row['call_id']
    return role, row.get('round_id'), row.get('subbasin_id'), tuple(row.get('attempt_ids') or [])


def _slot_key(row):
    return row.get('round_id'), row.get('subbasin_id'), row.get('candidate_id', 1)


def _failure_kinds(row, attempt_errors):
    kinds = set()
    for aid in row.get('attempt_ids') or []:
        for event in attempt_errors.get(aid, []):
            stage = event.get('stage')
            detail = str(event.get('validation_detail') or '').lower()
            if stage == 'backend_invocation':
                kinds.add('transport')
            elif stage == 'parse_or_schema':
                if 'unparsed json' in detail:
                    kinds.add('parse')
                elif 'truncated response' in detail:
                    kinds.add('truncated_response')
                elif 'did not finish normally' in detail:
                    kinds.add('response_completion')
                else:
                    kinds.add('schema_or_response_validation')
    if not kinds:
        if row.get('slot_recovery'):
            kinds.add('proposal_exception_or_invalid_candidate')
        else:
            kinds.add(row.get('failure_class') or 'unclassified')
    return sorted(kinds)


def summarize_recovery(output_root):
    """Deduplicate logical failures and replacement slots; never add the two counts.

    Candidate traces identify outer-MC substitutions absent from the call ledger.
    A call without a known source remains unlinked, never invented as a slot.
    """
    root = Path(output_root)
    logical, slots, attempt_errors = {}, {}, {}
    for row in _rows(root / 'llm_calls.jsonl'):
        if row.get('event') == 'attempt_error':
            attempt_errors.setdefault(row.get('attempt_id'), []).append(row)
    for row in _rows(root / 'llm_recoveries.jsonl'):
        if row.get('status') == 'historical_fallback_selected':
            logical.setdefault(_logical_key(row), row)
        elif row.get('status') == 'worker_code_candidate_evaluated':
            slots[_slot_key(row)] = row
    # Trace records share stable execution-slot identities.
    for row in _rows(root / 'process_trace.jsonl'):
        if row.get('event') != 'candidate_process' or row.get('branch') not in _RECOVERY_BRANCHES:
            continue
        proposal = row.get('proposal_trace')
        proposal = proposal if isinstance(proposal, dict) else {}
        recovery = proposal.get('recovery') or {}
        slots.setdefault(_slot_key(row), {
            'role': 'Worker', 'round_id': row.get('round_id'), 'phase': row.get('phase'),
            'subbasin_id': row.get('subbasin_id'), 'candidate_id': row.get('candidate_id', 1),
            'branch': row.get('branch'), 'fallback_source': row.get('candidate_source') or row.get('branch'),
            'reason': row.get('fallback_reason') or proposal.get('fallback_reason'),
            'call_id': proposal.get('call_id'), 'attempt_ids': proposal.get('attempt_ids', []),
            'failure_class': proposal.get('failure_class') or recovery.get('failure_class'),
            'slot_recovery': row.get('slot_recovery'), 'model_evaluation_ok': row.get('ok'),
            'recovery_origin': 'graph_mc_outer' if row.get('slot_recovery') else 'worker_agent',
            'recovery_scope': 'legacy_candidate_trace',
        })
    leaders = [row for row in logical.values() if str(row.get('role', '')).lower() == 'leader']
    workers = [row for row in logical.values() if str(row.get('role', '')).lower() == 'worker']
    by_call = {row.get('call_id'): row for row in workers if row.get('call_id')}
    for row in slots.values():
        parent = by_call.get(row.get('call_id'), {})
        for key in ('failure_class', 'reason', 'attempt_ids'):
            if not row.get(key):
                row[key] = parent.get(key)
    linked = {row.get('call_id') for row in slots.values() if row.get('call_id')}
    unlinked = [row for row in workers if not row.get('call_id') or row['call_id'] not in linked]
    decisions = leaders + list(slots.values())
    affected = decisions + unlinked
    rounds = sorted({row['round_id'] for row in affected if isinstance(row.get('round_id'), int)})
    stations = sorted({row['subbasin_id'] for row in affected if row.get('subbasin_id')})
    causes, reasons, sources = Counter(), Counter(), Counter()
    for row in decisions:
        row['failure_kinds'] = _failure_kinds(row, attempt_errors)
        causes.update(row['failure_kinds'])
        reasons[row.get('reason') or 'unclassified'] += 1
        sources[row.get('fallback_source') or row.get('branch') or 'historical_rule_fallback'] += 1
    compact_keys = ('round_id', 'phase', 'subbasin_id', 'candidate_id', 'call_id',
                    'branch', 'reason', 'failure_class', 'failure_kinds', 'fallback_source', 'recovery_origin')
    compact = lambda row: {key: row.get(key) for key in compact_keys if key in row}
    return {
        'protocol': 'llm_recovery_accounting_v1',
        'degraded': bool(logical or slots),
        'historical_fallback_calls': len(logical),
        'leader_logical_fallbacks': len(leaders),
        'worker_fallback_logical_calls': len(workers),
        'worker_code_substitution_slots': len(slots),
        'worker_call_recoveries_without_linked_slot': len(unlinked),
        'unlinked_worker_call_records': [compact(row) for row in unlinked],
        'affected_round_ids': rounds, 'affected_subbasin_ids': stations,
        'failure_kind_counts': dict(causes), 'reason_counts': dict(reasons),
        'replacement_source_counts': dict(sources),
        'leader_calls': [compact(row) for row in leaders],
        'worker_slots': [compact(row) for row in sorted(slots.values(), key=lambda x: (x.get('round_id') or 0, x.get('subbasin_id') or '', x.get('candidate_id') or 0))],
        'count_semantics': 'Leader counts logical fallback decisions; Worker counts evaluated code replacement slots. '
            'MC slots count separately. Call and slot counts overlap and must not be added. '
            'Normal SADR rules/escape are excluded. Retry attempts later accepted are not degradation. '
            'Multiple failure kinds can describe one replacement; transport is an API boundary classification, not a proven network cause.',
        'legacy_scope': 'Old calls without an execution-slot trace stay unlinked; no slot is inferred.',
    }
