"""Diagnostic rules and cross-basin coordination with finite-value, missing-station and phase guards."""
from dataclasses import asdict
from .diagnostic_evidence import finite, fmt, metric_lines
from .prompt_source import get_text
from .prompt_visibility import worker_reason

ATTRIBUTION_KIND = 'historical_policy_heuristic_not_causal_estimate'


def policy_diagnosis(metrics, previous=None, context=None):
    from .rule_diagnostics import diagnose_round
    from .worker_diagnostics import DiagnosisResult
    context = dict(context or {})
    if context.get('simulation_ok') is False:
        return DiagnosisResult(primary_issue='unknown', severity='unknown',
            improvement_trend='unknown', diagnostic_status='unavailable',
            reasoning='Simulation failed; no diagnostic priority or parameter action is inferred.',
            issue_status={key: 'unknown' for key in ('volume', 'timing', 'shape', 'baseflow')},
            unavailable_metrics=list(metrics), attribution_kind=ATTRIBUTION_KIND)
    # The rule module handles individual nonfinite or missing metrics.
    result = diagnose_round(metrics, previous, context)
    return DiagnosisResult(**asdict(result), attribution_kind=ATTRIBUTION_KIND)


def _get(diag, key, default=None):
    return diag.get(key, default) if isinstance(diag, dict) else getattr(diag, key, default)


def policy_worker_lines(diag, include_notice=True, static_input_protocol=None, prompt_visibility_protocol=None):
    """Project the saved diagnosis into Worker prompt lines."""
    lines = [get_text('historical_policy_notice', protocol=static_input_protocol)] if include_notice else []
    if not diag or _get(diag, 'diagnostic_status') == 'unavailable':
        return lines + ['Issue: unknown (unknown); heuristic priorities unavailable.',
                        worker_reason(_get(diag, 'reasoning', 'Diagnostic evidence unavailable.'))]
    lines.append(f"Issue: {_get(diag, 'primary_issue', 'unknown')} ({_get(diag, 'severity', 'unknown')}), "
                 f"VIC={_get(diag, 'vic_responsibility', 0):.0%} Rout={_get(diag, 'routing_responsibility', 0):.0%}")
    lines.append(worker_reason(_get(diag, 'reasoning', '')))
    return lines


def policy_leader_lines(diag, active_params=None, include_notice=True, static_input_protocol=None, prompt_visibility_protocol=None):
    """List the first three rule hints, then mark those outside the active phase."""
    lines = [get_text('historical_policy_notice', protocol=static_input_protocol)] if include_notice else []
    if not diag or _get(diag, 'diagnostic_status') == 'unavailable':
        return lines + ['Diagnosis: unknown; heuristic priorities and actions unavailable.',
                        'Reasoning: ' + str(_get(diag, 'reasoning', 'Diagnostic evidence unavailable.'))]
    lines.append(f"Diagnosis: {_get(diag, 'primary_issue', 'unknown')} issue, "
                 f"severity={_get(diag, 'severity', 'unknown')}, "
                 f"VIC priority={_get(diag, 'vic_responsibility', 0):.0%}, "
                 f"Routing priority={_get(diag, 'routing_responsibility', 0):.0%}")
    lines.append('Reasoning: ' + str(_get(diag, 'reasoning', '')))
    active = None if active_params is None else set(active_params)
    records = (_get(diag, 'recommendations', []) or [])[:3]
    if records:
        rendered = []
        for rec in records:
            inactive = get_text('historical_inactive_notice', protocol=static_input_protocol) if active is not None and rec['param'] not in active else ''
            rendered.append(f"{rec['param']} {rec['direction']}({rec['magnitude']}){inactive}")
        lines.append('Diag hints: ' + '; '.join(rendered))
    return lines


def policy_coordination(upstream_map, outlet_id, summaries):
    """Cross-station rules, excluding claims about stations without valid evidence."""
    from .cross_station_rules import LeaderCoordinator
    from .leader_coordinator import CoordinationResult
    sanitized = {}
    for original in summaries:
        sid = original.get('subbasin_id')
        if not sid:
            continue
        row = dict(original)
        score = None if row.get('simulation_ok') is False else finite(row.get('current_nse'))
        row['current_nse'] = score
        if score is None:
            row.update(primary_issue='unknown', routing_responsibility=0,
                       top_recommendations=[], suggested_direction='', stagnation_risk=False,
                       stagnation_count=0)
        sanitized[sid] = row
    # Explicit None prevents missing NSE from being treated as poor-upstream evidence.
    for sid in set(upstream_map) | {u for ups in upstream_map.values() for u in ups}:
        sanitized.setdefault(sid, dict(subbasin_id=sid, current_nse=None,
            primary_issue='unknown', routing_responsibility=0, top_recommendations=[],
            stagnation_risk=False, stagnation_count=0))
    rule_coordinator = LeaderCoordinator(upstream_map, outlet_id)
    original = rule_coordinator.compute_cross_basin_insights(list(sanitized.values()))
    # Cluster and routing rules name all configured upstream stations. Drop a
    # multi-station claim when any named station lacks valid evidence; otherwise
    # keep the insights in their computed order.
    unavailable = {sid for sid, row in sanitized.items() if row['current_nse'] is None}
    insights = [item for item in original.insights if not unavailable.intersection(item.affected_basins)]
    # Compute mean and outlet NSE from the shared facts; zero is a valid value.
    values = [sanitized[sid]['current_nse'] for sid in upstream_map]
    complete = bool(values) and all(value is not None for value in values)
    mean = sum(values) / len(values) if complete else None
    outlet = sanitized.get(outlet_id, {}).get('current_nse')
    compact = rule_coordinator._format_compact_summary(list(sanitized.values()), insights, mean, outlet)
    return CoordinationResult(insights, compact,
        original.global_status if complete else 'unavailable (missing/failed/nonfinite station)',
        mean, outlet, original.n_stagnant)


def policy_leader_block(reports, coordination, j_history, nse_history=None, active_params=None, static_input_protocol=None, prompt_visibility_protocol=None):
    """Station report layout with shared numerical evidence facts."""
    lines = [get_text('metric_definitions', protocol=static_input_protocol), get_text('historical_policy_notice', protocol=static_input_protocol), '## Global Status',
        f'Mean NSE={fmt(coordination.mean_nse)}; outlet NSE={fmt(coordination.outlet_nse)}',
        f'Global trend (heuristic): {coordination.global_status}']
    if j_history:
        lines.append('J_total trend (last rounds): ' + ' -> '.join(fmt(v) for v in j_history[-5:]))
    lines.append('\n## Per-Basin Diagnostics')
    for report in reports:
        sid = report.get('subbasin_id', '?')
        failed = report.get('simulation_ok') is False
        metrics = {} if failed else report.get('current_metrics', {})
        rs, ledger = report.get('round_summary', {}), report.get('ledger_summary', {})
        lines += ['\n### ' + sid] + metric_lines(metrics, static_input_protocol)
        lines += [f'Model evaluation status: {report.get("simulation_ok", "not supplied")}',
            f'Diagnosed accepted-state parameters: {report.get("evaluated_params")}',
            f'Accepted center after round: {report.get("accepted_center_after_round")}']
        provenance = report.get('diagnostic_evaluation_record', {})
        if provenance:
            lines.append(f"Diagnostic observation round={provenance.get('observed_round_id')}; "
                         f"evaluation round={provenance.get('evaluation_round_id', provenance.get('round_id'))}; "
                         f"phase={provenance.get('phase')}; execution={provenance.get('execution_identity')}; "
                         f"upstream identity={provenance.get('upstream_context_id')}")
        if failed:
            lines.append('Round diagnostic summary unavailable: model evaluation failed.')
        else:
            lines.append(f"summary: Δnse={rs.get('nse_delta', '?')}, timing={rs.get('timing_status', '?')}, "
                         f"recession={rs.get('recession_status', '?')}, stagnant={rs.get('is_stagnant', False)}")
            lines.append('Δnse compares accepted station states across rounds; upstream may differ. '
                         'Use selected-candidate feedback for the same-background local comparison.')
        lines += policy_leader_lines(None if failed else report.get('diagnosis'), active_params, False, static_input_protocol, prompt_visibility_protocol)
        lines.append(f"Trend: {report.get('diagnosis', {}).get('improvement_trend', '?')}, "
                     f"stagnation={ledger.get('stagnation_count', 0)} rounds, best NSE={fmt(report.get('best_nse'))}")
        scope = report.get('stagnation_scope', {})
        lines.append(f"Stagnation through round {scope.get('through_round_id', 'not supplied')}: "
                     'no NSE exceeding historical best + 0.001; not rejected-candidate count.')
        if nse_history and sid in nse_history:
            lines.append('NSE trajectory: ' + ' -> '.join(fmt(v) for v in nse_history[sid][-5:]))
    if coordination.insights:
        lines.append('\n## Cross-Basin Insights (code heuristic hypotheses)')
        for index, insight in enumerate(coordination.insights[:5], 1):
            lines += [f'{index}. [{insight.severity}] {insight.description}',
                      'Recommendation: ' + insight.recommendation]
        lines.append(get_text('historical_fixed_phase_note', protocol=static_input_protocol))
    lines += ['\n## Provide per-basin parameter adjustment instructions',
              get_text('historical_leader_task', protocol=static_input_protocol)]
    return '\n'.join(lines)


def policy_exploration_summary(results, static_input_protocol=None):
    """Six-parameter top-10/bottom-10 association summary, without imputation."""
    lines = [get_text('historical_exploration_header', protocol=static_input_protocol)]
    for sid, result in results.items():
        ordered = sorted([sample for sample in result.get('all_results', [])
            if sample.get('ok') is not False and finite(sample.get('nse')) is not None],
            key=lambda sample: float(sample['nse']), reverse=True)
        if len(ordered) < 10:
            lines.append(f'{sid}: valid={len(ordered)}; insufficient samples for top10/bottom10 directions')
            continue
        hints = []
        for key in ('binfilt', 'Ds', 'Dsmax', 'Ws', 'soil_d2', 'soil_d3'):
            top = [finite(sample.get('params', {}).get(key)) for sample in ordered[:10]]
            bottom = [finite(sample.get('params', {}).get(key)) for sample in ordered[-10:]]
            if any(value is None for value in top + bottom):
                continue
            difference = sum(top) / 10 - sum(bottom) / 10
            if abs(difference) > .01:
                hints.append(f"{key}={'higher' if difference > 0 else 'lower'}")
        overlap = '; top10/bottom10 coincide' if len(ordered) == 10 else ('; top10/bottom10 overlap' if len(ordered) < 20 else '')
        lines.append(f"{sid}: valid={len(ordered)}, best={ordered[0]['nse']:.3f}, "
            f"top10 range={ordered[9]['nse']:.3f}-{ordered[0]['nse']:.3f}, good direction (association hypothesis): "
            + (', '.join(hints) or 'no clear pattern') + overlap)
    return '\n'.join(lines)
