"""Phase-scoped suggestions and exact MC facts; no search or acceptance policy."""
from __future__ import annotations

import copy
import json
import math
import re

from .factual_context import active_keys

PARAMETERS = ('binfilt', 'Ds', 'Dsmax', 'Ws', 'soil_d2', 'soil_d3', 'velocity', 'diffusivity')
_PARAMETER_PATTERN = re.compile(r'(?<![A-Za-z0-9_])(' + '|'.join(PARAMETERS) + r')(?![A-Za-z0-9_])', re.I)
_CANONICAL = {name.lower(): name for name in PARAMETERS}


def phase_aware_insight(insight, decision_target):
    """Label an unavailable action as background for the actual decision phase."""
    shown = copy.deepcopy(insight)
    active = set(decision_target.get('active_parameters', ()))
    recommendation = shown.get('recommendation', '')
    mentioned = {_CANONICAL[m.group(1).lower()] for m in _PARAMETER_PATTERN.finditer(recommendation)}
    blocked = [p for p in PARAMETERS if p in mentioned - active]
    terminal = decision_target.get('purpose') == 'terminal_review_only'
    if recommendation and (blocked or terminal):
        shown['background_recommendation'] = shown.pop('recommendation')
        shown['action_status'] = 'background_only_for_decision_target'
        if blocked:
            shown['inactive_parameters'] = blocked
    return shown


def _valid_nse(record):
    nse = (record.get('metrics') or {}).get('NSE')
    return nse if record.get('simulation_ok') is True and isinstance(nse, (float, int)) and not isinstance(nse, bool) and math.isfinite(nse) else None


def mc_candidate_feedback(records, selected_id):
    """Expose effective active parameters and exact scores, not proposed values."""
    rows = []
    for record in records:
        params = record.get('effective_params') or {}
        row = {'candidate_id': record['candidate_id'],
               'NSE': (record.get('metrics') or {}).get('NSE'),
               'simulation_ok': record.get('simulation_ok'),
               'selected': record['candidate_id'] == selected_id,
               'effective_active_params': {key: params[key] for key in active_keys(record.get('phase')) if key in params},
               'upstream_context_id': record.get('upstream_context_id')}
        rows.append(row)
    return rows


def mc_selection_feedback(records, selected_id):
    """Describe exact numerical ties; the caller has already selected its slot."""
    valid = [(r['candidate_id'], _valid_nse(r)) for r in records]
    valid = [(cid, nse) for cid, nse in valid if nse is not None]
    best = max((nse for _, nse in valid), default=None)
    tied = [cid for cid, nse in valid if nse == best]
    groups = {}
    for record in records:
        # A duplicate requires the complete effective vector and its background.
        params = record.get('effective_params')
        upstream = record.get('upstream_context_id')
        if not params or upstream is None:
            continue
        key = json.dumps([record.get('phase'), params, upstream], sort_keys=True, allow_nan=False)
        groups.setdefault(key, []).append(record['candidate_id'])
    return {'selected_candidate_id': selected_id,
            'selection_status': ('all_scores_unavailable' if not valid else
                                 'exact_maximum_tie' if len(tied) > 1 else 'unique_maximum'),
            'equal_maximum_candidate_ids': tied,
            'identical_effective_candidate_groups': [ids for ids in groups.values() if len(ids) > 1],
            **({'tie_break': 'first_evaluated_candidate'} if len(tied) > 1 else {})}


def format_mc_summary(results, selected):
    """Format an already selected MC result without claiming tied superiority."""
    records = [r['experiment'] for r in sorted(results, key=lambda r: r['idx'])]
    selection = mc_selection_feedback(records, selected['experiment']['candidate_id'])
    labels = {r['experiment']['candidate_id']: r['label'] for r in results}
    status = selection['selection_status']
    if status == 'unique_maximum':
        return f"best={selected['label']}"
    tied = ','.join(labels[cid] for cid in selection['equal_maximum_candidate_ids'])
    detail = f"exact_maximum_tie={tied}; first_evaluated_tie_break" if tied else 'all_scores_unavailable'
    duplicates = selection['identical_effective_candidate_groups']
    if duplicates:
        detail += '; identical_effective=' + '/'.join(','.join(labels[cid] for cid in ids) for ids in duplicates)
    return f"selected={selected['label']}; {detail}"


def compact_mc_history(feedbacks):
    """Render retained MC attempts with round/station identity and local aliases."""
    rows = []
    upstream_labels = {}
    columns_by_phase = {}
    for feedback in feedbacks:
        if not feedback or len(feedback.get('mc_candidates', [])) < 2:
            continue
        source = feedback['mc_candidates']
        phase = feedback.get('phase')
        keys = list(active_keys(phase))
        columns_by_phase[phase] = keys
        backgrounds = [c.get('upstream_context_id') for c in source]
        shared = backgrounds[0] is not None and all(x == backgrounds[0] for x in backgrounds)
        def upstream_alias(value):
            if value is not None and value not in upstream_labels:
                upstream_labels[value] = f'mcU{len(upstream_labels) + 1}'
            return upstream_labels.get(value, 'unknown')
        ids = {c['candidate_id']: i + 1 for i, c in enumerate(source)}
        candidates = []
        for index, candidate in enumerate(source, 1):
            values = [candidate.get('effective_active_params', {}).get(key) for key in keys]
            row = [index, values, candidate.get('NSE'), candidate.get('simulation_ok'), candidate.get('selected')]
            if not shared:
                row.append(upstream_alias(candidate.get('upstream_context_id')))
            candidates.append(row)
        selection = feedback.get('mc_selection') or {}
        result = {'round_id': feedback.get('round_id'), 'station': feedback.get('subbasin_id'),
                  'phase': phase, 'candidates': candidates,
                  'selection_status': selection.get('selection_status')}
        if shared:
            result['upstream'] = upstream_alias(backgrounds[0])
        if len(selection.get('equal_maximum_candidate_ids', [])) > 1:
            result['exact_maximum_tie'] = [ids[cid] for cid in selection['equal_maximum_candidate_ids']]
        duplicates = selection.get('identical_effective_candidate_groups')
        if duplicates:
            result['identical_effective_groups'] = [[ids[cid] for cid in group] for group in duplicates]
        rows.append(result)
    if not rows:
        return ''
    return json.dumps({'candidate_columns': ['slot', 'effective_values', 'NSE', 'ok', 'selected', 'upstream_if_not_shared'],
                       'parameter_columns_by_phase': columns_by_phase,
                       'identity': 'candidate=R{round_id}:{station}:c{slot}; mcU compares only within this history',
                       'rounds': rows}, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
