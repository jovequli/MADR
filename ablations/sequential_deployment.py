"""Deployment receipts for the opt-in station-by-station workflow.

The ordinary launcher remains unchanged. Local round identifiers are never
mistaken for simultaneous global rounds when auditing usage or completion.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

ARM = 'A3_strict_sequential'


def selected(manifest):
    return manifest.get('budget', {}).get('ablation_protocol', {}).get('arm') == ARM


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def validate_completion(work, manifest, exit_code):
    from .search_interventions import evaluation_schedule
    from scripts.recovery_accounting import summarize_recovery

    work = Path(work)
    config = _read(work / manifest['config_relative'])
    output = work / 'runs' / manifest['run_name']
    outer_path = output / 'final_summary.json'
    outer = _read(outer_path) if outer_path.is_file() else {}
    rows = evaluation_schedule(config)
    expected_rounds = len(rows)
    errors = []
    if exit_code != 0:
        errors.append('framework_exit_nonzero')
    if outer.get('audit_complete') is not True:
        errors.append('sequential_audit_incomplete')
    stations = {}
    for worker in config['workers']:
        sid = worker['subbasin_id']
        root = output / 'stations' / sid
        path = root / 'final_summary.json'
        summary = _read(path) if path.is_file() else {}
        local_errors = []
        if summary.get('audit_complete') is not True:
            local_errors.append('core_audit_incomplete')
        if summary.get('rounds_executed') != expected_rounds:
            local_errors.append('round_budget_incomplete')
        if summary.get('final_confirmation_round') != expected_rounds:
            local_errors.append('force_best_confirmation_missing')
        for row in rows:
            path = root / f"round_{row['round_id']:03d}" / 'round_meta.json'
            meta = _read(path) if path.is_file() else {}
            if meta.get('round_id') != row['round_id'] or meta.get('phase') != row['phase']:
                local_errors.append('phase_or_round_identity_incomplete')
                break
        recovery = summarize_recovery(root)
        stations[sid] = {
            'complete': not local_errors, 'errors': local_errors,
            'rounds_executed': summary.get('rounds_executed'),
            'final_confirmation_nse': summary.get('final_confirmation_mean_nse'),
            'recovery': recovery,
        }
        errors.extend(f'{sid}:{error}' for error in local_errors)
    executed = sum(row['rounds_executed'] or 0 for row in stations.values())
    if outer.get('rounds_executed') != executed:
        errors.append('aggregate_round_accounting_mismatch')
    return {
        'complete': not errors, 'errors': errors,
        'historical_fallback_calls': sum(row['recovery']['historical_fallback_calls'] for row in stations.values()),
        'degraded': any(row['recovery']['degraded'] for row in stations.values()),
        'llm_recovery_accounting': {'by_station': {sid: row['recovery'] for sid, row in stations.items()},
                                    'round_identity': '(station, local_round_id)'},
        'core_audit_complete': outer.get('audit_complete'),
        'rounds_executed': executed,
        'round_count_semantics': 'Sequential station-local interaction rounds, summed across stations.',
        'best_mean_nse': outer.get('best_mean_nse'),
        'final_confirmation_mean_nse': outer.get('final_confirmation_mean_nse'),
        'stations': stations,
    }


def summarize(spec, manifest, deployment):
    import summarize_token_usage as usage
    from .search_interventions import evaluation_schedule

    work = spec['work']
    config = _read(work / manifest['config_relative'])
    output = work / 'runs' / manifest['run_name']
    station_reports, attempts, missing, unmeasured = {}, [], [], []
    for worker in config['workers']:
        sid = worker['subbasin_id']
        root = output / 'stations' / sid
        ledger = root / 'llm_calls.jsonl'
        if not ledger.is_file():
            missing.append(sid)
            continue
        child_path = output / 'station_configs' / f'{sid}.json'
        if not child_path.is_file():
            raise ValueError('Missing station execution configuration: ' + sid)
        child = _read(child_path)
        report = usage.audit_file(ledger, schedule=evaluation_schedule(child), method=config['method'])
        station_reports[sid] = report
        for original in report['attempts']:
            row = copy.deepcopy(original)
            row['station_run'] = sid
            row['local_round_id'] = row.get('round_id')
            row['call_id'] = f"{sid}:{row['call_id']}"
            row['attempt_id'] = f"{sid}:{row['attempt_id']}"
            attempts.append(row)
        unmeasured.extend(f'{sid}:{identity}' for identity in report['logical_requests_without_recorded_attempt'])
    if not station_reports:
        raise ValueError('No station API attempt ledger exists yet')
    totals = usage.aggregate(attempts)
    providers = {row.get('provider') for row in attempts if row.get('provider')}
    price = usage.user_price_reference(totals, next(iter(providers))) if len(providers) == 1 else None
    if price is not None:
        price['logical_requests_without_attempt_usage'] = len(unmeasured)
        if unmeasured or missing:
            price['complete_cost_under_reference_tariff'] = False
            price['total_usd_if_complete'] = None
            for scenario in price.get('scenarios', {}).values():
                scenario['total_usd_if_complete'] = None
        totals['user_price_reference'] = price
    report = {
        'schema': 'madr-sequential-token-audit-v1', 'totals': totals,
        'schedule_source': 'per_station_frozen_team_json',
        'by_station': station_reports, 'missing_station_ledgers': missing,
        'logical_requests_without_recorded_attempt': unmeasured,
        'coverage': 'Observed station ledgers only; missing stations and unknown usage are not free calls.',
        'planned_budget': manifest['budget'],
        'round_identity': '(station_run, local_round_id)',
    }
    reports = work / 'reports'
    reports.mkdir(exist_ok=True)
    deployment.write_json(reports / 'token_usage.json', report)
    result = {
        'release': deployment.RELEASE, 'work_root': str(work),
        'schedule_source': report['schedule_source'],
        'token_report': str(reports / 'token_usage.json'), 'actual_usage': totals,
        'final_summary': str(output / 'final_summary.json'),
        'native_audit': 'Each station retains its own native_audit directory.',
        'missing_station_ledgers': missing,
        'candidate_and_attempt_accounting': 'Physical calls, candidate slots and LLM attempts remain distinct; station-local round identifiers are not merged.',
    }
    deployment.write_json(reports / 'standalone_summary.json', result)
    return result
