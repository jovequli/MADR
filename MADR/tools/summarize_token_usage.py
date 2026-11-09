#!/usr/bin/env python3
"""Read saved API attempts; never make requests or infer tokens from characters.

Usage is counted once per attempt_response, including rejected/truncated replies.
Cache hits are a subset of input; reasoning tokens are a subset of output.
Transport attempts without provider usage remain explicitly unmeasured.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

TOKEN_FIELDS = ('input_tokens', 'output_tokens_including_thinking', 'total_tokens',
                'thinking_tokens', 'visible_output_tokens', 'cache_hit_tokens', 'cache_miss_tokens',
                'cache_creation_tokens', 'uncached_input_tokens')


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def normalize_usage(event):
    u = event.get('usage_provider_reported')
    if not isinstance(u, dict):
        return None
    provider=event.get('provider')
    if provider=='anthropic':
        uncached=number(u.get('input_tokens'));out=number(u.get('output_tokens'))
        if uncached is None or out is None:return None
        # These three input categories are disjoint. Missing/invalid cache
        # counters cannot certify zero cache or a complete full-input total.
        create=number(u.get('cache_creation_input_tokens'))
        hit=number(u.get('cache_read_input_tokens'))
        inp=uncached+create+hit if create is not None and hit is not None else None
        details=u.get('output_tokens_details')
        raw_thinking=details.get('thinking_tokens') if isinstance(details,dict) else None
        thinking=number(raw_thinking);issues=[]
        if raw_thinking is not None and thinking is None:
            issues.append('invalid_thinking_token_count')
        if thinking is not None and thinking>out:
            issues.append('thinking_exceeds_completion');thinking=None
        return {'input_tokens':inp,'output_tokens_including_thinking':out,'total_tokens':inp+out if inp is not None else None,
                'thinking_tokens':thinking,'visible_output_tokens':out-thinking if thinking is not None else None,
                'cache_hit_tokens':hit,'cache_miss_tokens':uncached+create if create is not None else None,
                'cache_creation_tokens':create,'uncached_input_tokens':uncached,'issues':issues,
                'cache_observation':{'read':counter_state(u,'cache_read_input_tokens'),
                                     'write':counter_state(u,'cache_creation_input_tokens')},
                'semantics':'Anthropic full input=uncached+cache_creation+cache_read; output already includes thinking; optional output_tokens_details.thinking_tokens is a subset, absent split remains unknown'}
    if provider in ('gemini','google'):
        inp=number(u.get('prompt_token_count'));visible=number(u.get('candidates_token_count'))
        think=number(u.get('thoughts_token_count'));reported_total=number(u.get('total_token_count'))
        hit=number(u.get('cached_content_token_count'))
        if inp is None or visible is None:return None
        # When the split is omitted, retain the provider total as the output
        # aggregate only if no tool-use prompt tokens were reported.
        tool=number(u.get('tool_use_prompt_token_count')) or 0
        out=visible+think if think is not None else (reported_total-inp if reported_total is not None and not tool else None)
        if out is None or out<visible:return None
        issues=[]
        if tool:issues.append('tool_use_prompt_tokens_present_review_total')
        if reported_total is not None and reported_total!=inp+out:issues.append('reported_total_does_not_equal_input_plus_output')
        if hit is not None and hit>inp:issues.append('cache_partition_invalid');hit=None
        return {'input_tokens':inp,'output_tokens_including_thinking':out,'total_tokens':inp+out,
                'thinking_tokens':think,'visible_output_tokens':visible,
                'cache_hit_tokens':hit,'cache_miss_tokens':inp-hit if hit is not None else None,
                'cache_creation_tokens':0,'uncached_input_tokens':inp-hit if hit is not None else None,'issues':issues,
                'cache_observation':{'read':counter_state(u,'cached_content_token_count'),'write':'not_applicable'},
                'semantics':'Gemini output=candidates+thoughts; prompt already includes cached content'}
    if provider!='deepseek':raise ValueError('Unsupported raw-usage provider: '+str(provider))
    inp, out = number(u.get('prompt_tokens')), number(u.get('completion_tokens'))
    if inp is None or out is None:
        return None
    reported_total = number(u.get('total_tokens'))
    hit = number(u.get('prompt_cache_hit_tokens'))
    if hit is None:
        hit = number((u.get('prompt_tokens_details') or {}).get('cached_tokens'))
    miss = number(u.get('prompt_cache_miss_tokens'))
    if hit is None and miss is not None:
        hit = inp - miss
    if miss is None and hit is not None:
        miss = inp - hit
    thinking = number((u.get('completion_tokens_details') or {}).get('reasoning_tokens'))
    issues = []
    if reported_total is not None and reported_total != inp + out:
        issues.append('reported_total_does_not_equal_input_plus_output')
    if hit is not None and (hit < 0 or miss < 0 or hit + miss != inp):
        issues.append('cache_partition_invalid')
        hit = miss = None
    if thinking is not None and thinking > out:
        issues.append('thinking_exceeds_completion')
        thinking = None
    return {'input_tokens': inp, 'output_tokens_including_thinking': out,
            'total_tokens': inp + out, 'thinking_tokens': thinking,
            'visible_output_tokens': out - thinking if thinking is not None else None,
            'cache_hit_tokens': hit, 'cache_miss_tokens': miss, 'cache_creation_tokens':0,
            'uncached_input_tokens':miss, 'issues': issues,
            'cache_observation':{'read':('invalid' if 'cache_partition_invalid' in issues else
                counter_state(u,'prompt_cache_hit_tokens') if 'prompt_cache_hit_tokens' in u else
                counter_state(u.get('prompt_tokens_details') or {},'cached_tokens') if 'cached_tokens' in (u.get('prompt_tokens_details') or {}) else
                'derived_from_reported_miss' if hit is not None else 'missing'), 'write':'not_applicable'}}


def counter_state(mapping, key):
    if key not in mapping or mapping[key] is None:return 'missing'
    value=number(mapping[key])
    return 'invalid' if value is None else 'reported_zero' if value==0 else 'reported_positive'


def prompt_characters(request):
    """Count text actually retained in the request; never estimate tokens here."""
    if not isinstance(request,dict):return None
    def text_size(value):
        if isinstance(value,str):return len(value)
        if isinstance(value,list):return sum(text_size(v) for v in value)
        if isinstance(value,dict):return text_size(value.get('text',value.get('content','')))
        return 0
    return text_size(request.get('system',''))+text_size(request.get('messages',[]))


def aggregate(rows):
    measured = [r for r in rows if r.get('usage') is not None]
    complete = [r for r in measured if all(r['usage'].get(k) is not None for k in
                ('input_tokens','output_tokens_including_thinking','total_tokens'))]
    sums, unknown = {}, {}
    for key in TOKEN_FIELDS:
        available = [r['usage'].get(key) for r in measured if r['usage'].get(key) is not None]
        sums[key] = sum(available)
        unknown[key] = len(rows) - len(available)
    durations = [r['duration_sec'] for r in rows if r['duration_sec'] is not None]
    hit_input = sum(r['usage']['input_tokens'] for r in measured if r['usage']['cache_hit_tokens'] is not None and r['usage']['input_tokens'] is not None)
    hit_subset = sum(r['usage']['cache_hit_tokens'] for r in measured if r['usage']['cache_hit_tokens'] is not None and r['usage']['input_tokens'] is not None)
    prompt_sizes = [r['prompt_characters'] for r in rows if r.get('prompt_characters') is not None]
    logical = len({r['call_id'] for r in rows})
    return {'logical_calls': logical, 'backend_attempts': len(rows),
            'extra_attempts_beyond_one_per_call': len(rows) - logical,
            'response_attempts': sum(r['responded'] for r in rows),
            'rejected_or_error_attempts': sum(r['failed'] for r in rows),
            'raw_usage_present_attempts':len(measured),
            'usage_measured_attempts': len(complete),
            'usage_unknown_attempts': len(rows) - len(complete),
            'usage_bearing_failed_attempts': sum(r['failed'] for r in measured),
            'known_token_sums': sums,
            'token_totals': {key: sums[key] if unknown[key] == 0 else None for key in TOKEN_FIELDS},
            'unknown_attempt_count_by_token_field': unknown,
            'cache_hit_fraction_on_cache_reported_input': hit_subset/hit_input if hit_input else None,
            'cache_observation_counts':{kind:dict(Counter(r['usage'].get('cache_observation',{}).get(kind,'unknown') if r.get('usage') else 'no_usage' for r in rows)) for kind in ('read','write')},
            'mean_input_per_measured_attempt': sums['input_tokens']/(len(rows)-unknown['input_tokens']) if len(rows)>unknown['input_tokens'] else None,
            'mean_output_per_measured_attempt': sums['output_tokens_including_thinking']/(len(rows)-unknown['output_tokens_including_thinking']) if len(rows)>unknown['output_tokens_including_thinking'] else None,
            'known_prompt_characters':sum(prompt_sizes), 'prompt_characters_unknown_attempts':len(rows)-len(prompt_sizes),
            'mean_prompt_characters_per_recorded_attempt':sum(prompt_sizes)/len(prompt_sizes) if prompt_sizes else None,
            'known_api_duration_sec': sum(durations), 'duration_unknown_attempts': len(rows)-len(durations),
            'mean_api_duration_sec': sum(durations)/len(durations) if durations else None,
            'finish_reasons': dict(Counter(r['finish_reason'] for r in rows)),
            'issues': [dict(attempt_id=r['attempt_id'], issues=r['usage']['issues'])
                       for r in measured if r['usage']['issues']]}


def group(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[str(key(row))].append(row)
    return {k: aggregate(v) for k, v in sorted(groups.items())}


def run_schedule(paths):
    """Use the schedule saved with the run."""
    for ancestor in (paths[0].parent, *list(paths[0].parents)[:4]):
        summary = ancestor / 'final_summary.json'
        if summary.is_file():
            rows = json.loads(summary.read_text()).get('execution_budget', {}).get('expected', {}).get('rounds')
            if rows:
                return rows, 'saved_final_summary', ancestor
        config_path = ancestor / 'provenance/config.json'
        if config_path.is_file():
            config = json.loads(config_path.read_text())
            pipeline = config.get('phase_config', {}).get('pipeline', [])
            total = sum(p['max_rounds'] for p in pipeline)
            schedule = []
            for phase in pipeline:
                for local in range(1, phase['max_rounds'] + 1):
                    rid = len(schedule) + 1
                    mc = local <= phase.get('mc_rounds', 0) and phase.get('mc_candidates', 0) > 0 and rid not in (1, total)
                    count = phase['mc_candidates'] if mc else 1
                    schedule.append({'round_id': rid, 'phase': phase['name'], 'per_station': count,
                                     'candidate_evaluations': len(config['workers']) * count})
            if schedule:
                return schedule, 'saved_provenance_config', ancestor
    return [], 'unknown_no_saved_schedule', paths[0].parent


def classify_context(context, schedule):
    by_round = {r['round_id']: r for r in schedule}
    role = str(context.get('role', 'unknown')).lower()
    rid = context.get('round_id')
    scheduled = by_round.get(rid, {})
    terminal = rid == max(by_round) if by_round else False
    round_mode = ('unknown' if not scheduled else 'default' if rid == min(by_round) else
                  'terminal_confirmation' if terminal else 'mc' if scheduled['per_station'] > 1 else 'single')
    observed = context.get('observed_phase') or context.get('phase') or 'unknown'
    target = context.get('decision_target_phase') or 'unknown'
    purpose = context.get('decision_purpose')
    transition = role == 'leader' and observed != 'unknown' and target != 'unknown' and observed != target
    if role == 'leader':
        stage = ('terminal_review' if purpose == 'terminal_review_only' or terminal else
                 'transition_review' if transition else 'default_review' if round_mode == 'default' else
                 'mc_review' if round_mode == 'mc' else 'single_review' if round_mode == 'single' else 'unknown_review')
        category = 'leader'
    elif role == 'worker':
        stage = 'worker_' + round_mode
        category = stage
    else:
        stage = category = role
    issues = []
    if role == 'worker' and round_mode in ('default', 'terminal_confirmation'):
        issues.append('unexpected_worker_request_in_non_llm_physical_round')
    if scheduled and observed != 'unknown' and observed != scheduled['phase']:
        issues.append('observed_phase_differs_from_saved_schedule')
    return {'role': role, 'category': category, 'stage': stage, 'round_id': rid,
            'phase': observed, 'observed_phase': observed,
            'observed_round_id': context.get('observed_round_id', rid),
            'decision_target_phase': target if role == 'leader' else None,
            'decision_target_round_id': context.get('decision_target_round_id') if role == 'leader' else None,
            'decision_purpose': purpose, 'phase_transition': transition, 'round_mode': round_mode,
            'scheduled_phase': scheduled.get('phase'), 'classification_issues': issues}


def evaluation_budget(schedule, method="MADR", config=None):
    """Upper bounds from the configured schedule, never measured native calls."""
    if not schedule:
        return {'scope': 'unknown: no saved schedule', 'rounds': []}
    final = max(r['round_id'] for r in schedule)
    config = config or {}; workers = config.get('workers', [])
    stations = {w['subbasin_id'] for w in workers}
    downstream = {sid for w in workers for sid in w.get('neighbors_downstream', []) if sid in stations}
    downstream_count = len(downstream) if workers else None
    rows = []
    for row in schedule:
        rid = row['round_id']; main = row['candidate_evaluations']
        centers = downstream_count if 1 < rid < final else 0
        rows.append({**row, 'center_reference_slots': centers,
                     'evaluation_attempts_upper_bound': None if centers is None else main + centers,
                     'expected_leader_logical_calls': 1,
                     'expected_worker_logical_calls': main if method == 'MADR' and 1 < rid < final else 0,
                     'physical_only_main_evaluations': main if method == 'SADR' or rid in (1, final) else 0})
    preparations = (len(workers) if workers else schedule[0]['candidate_evaluations'] // schedule[0]['per_station']) if any(row['phase']=='ROUTING_ONLY' for row in rows) else 0
    references = sum(r['center_reference_slots'] for r in rows) if downstream_count is not None else None
    main = sum(r['candidate_evaluations'] for r in rows)
    return {'scope': 'Planned main evaluations and maximum center-reference slots; reuse reduces actual model calls. Exploration excluded here.',
            'rounds': rows, 'fixed_flux_preparations_planned': preparations,
            'main_candidate_evaluations': main, 'center_reference_slots': references,
            'total_evaluation_attempts_upper_bound': None if references is None else main + references,
            'leader_logical_calls': len(rows),
            'worker_logical_calls': sum(r['expected_worker_logical_calls'] for r in rows)}


def observed_model_budget(root, config):
    """Read measured executor/process counters and separate zero-call reuse receipts."""
    root=Path(root); path=root/'native_audit/accounting.json'
    accounting=json.loads(path.read_text()).get('counts', {}) if path.is_file() else {}
    stations={w['subbasin_id'] for w in config.get('workers', [])}
    downstream={sid for w in config.get('workers', []) for sid in w.get('neighbors_downstream', []) if sid in stations}
    new=reused=slots=0
    for directory in sorted(root.glob('round_*/center_references')):
        for sid in downstream:
            record_path=directory/f'{sid}.json'
            if not record_path.is_file(): continue
            record=json.loads(record_path.read_text()); slots+=1
            additional=record.get('additional_model_evaluations')
            if additional==1: new+=1
            elif additional==0 and record.get('reference_reuse'): reused+=1
    return {'source':str(path) if path.is_file() else None,
            'candidate_evaluations':accounting.get('candidate_evaluations'),
            'actual_vic_launches':accounting.get('actual_vic_launches'),
            'actual_routing_launches':accounting.get('routing_launches'),
            'fixed_flux_preparation_attempts':accounting.get('cache_preparation_attempts'),
            'routing_flux_cache_hits':accounting.get('cache_hits'),
            'downstream_reference_records':slots, 'new_downstream_center_evaluations':new,
            'center_reference_reuses':reused,
            'scope':'Recorded process starts and evaluation attempts, including exploration and failures. Center reuse launches no process; routing flux-cache hits still require routing.'}


def audit_file(path, schedule=None, method=None):
    requests, responses, errors = {}, {}, defaultdict(list)
    logical_requests, logical_responses, logical_errors = {}, set(), set()
    event_counts = Counter()
    duplicates = 0
    paths = [Path(path)] if isinstance(path, (str, Path)) else [Path(p) for p in path]
    resolved, schedule_source, root = run_schedule(paths)
    config_path = root/'provenance/config.json'
    config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    method = method or config.get('method') or 'MADR'
    if method not in ('MADR', 'SADR'):
        raise ValueError('Unknown method for API-call budget')
    schedule = resolved if schedule is None else schedule
    if schedule is not resolved:
        schedule_source = 'explicit_schedule'
    for source in paths:
        for lineno, line in enumerate(source.read_text(encoding='utf-8').splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('not an event object')
            except (ValueError, TypeError) as exc:
                raise ValueError(f'{source}:{lineno}: invalid JSON event; do not report a partial ledger as complete') from exc
            kind = event.get('event')
            event_counts[kind] += 1
            call_id = event.get('call_id')
            if kind == 'request' and call_id:
                if call_id in logical_requests and logical_requests[call_id] != event:
                    raise ValueError(f'{source}:{lineno}: conflicting logical request {call_id}')
                logical_requests[call_id] = event
            if kind == 'response' and call_id:
                logical_responses.add(call_id)
            if kind == 'exception' and call_id:
                logical_errors.add(call_id)
            if kind not in ('attempt_request', 'attempt_response', 'attempt_error'):
                continue
            aid = event.get('attempt_id')
            if not aid:
                raise ValueError(f'{source}:{lineno}: attempt event lacks identity')
            if kind == 'attempt_error':
                if event not in errors[aid]:
                    errors[aid].append(event)
                else:
                    duplicates += 1
                continue
            target = requests if kind == 'attempt_request' else responses
            if aid in target:
                if target[aid] != event:
                    raise ValueError(f'{source}:{lineno}: conflicting duplicate attempt {aid}')
                duplicates += 1
            target[aid] = event
    rows = []
    for aid in sorted(set(requests) | set(responses) | set(errors)):
        req, resp = requests.get(aid, {}), responses.get(aid, {})
        base = resp or req or errors[aid][0]
        call_id = base.get('call_id') or req.get('call_id') or aid
        identities = {e.get('call_id') for e in [req, resp, *errors[aid]] if e.get('call_id')}
        if len(identities) > 1:
            raise ValueError('Attempt has conflicting logical-call identities: ' + str(aid))
        context = (req.get('context') or base.get('context') or
                   logical_requests.get(call_id, {}).get('context') or {})
        classification = classify_context(context, schedule)
        duration = number(resp.get('duration_sec'))
        if duration is None:
            duration = next((number(e.get('duration_sec')) for e in errors[aid]
                             if e.get('stage') == 'backend_invocation' and number(e.get('duration_sec')) is not None), None)
        rows.append({'attempt_id': aid, 'call_id': call_id,
                     'provider': base.get('provider') or req.get('provider'),
                     'model': resp.get('model_reported') or req.get('model_requested'),
                     **classification, 'subbasin_id': context.get('subbasin_id'),
                     'responded': bool(resp), 'failed': bool(errors[aid]),
                     'prompt_characters':prompt_characters(req.get('request')),
                     'prompt_cache_system_requested':req.get('controls',{}).get('prompt_cache_system'),
                     'controls':req.get('controls',{}),
                     'outer_attempt':req.get('outer_attempt',1),'transport_attempt':req.get('transport_attempt',1),
                     'duration_sec': duration, 'finish_reason': resp.get('finish_reason') or 'no_response',
                     'usage': normalize_usage({**req, **resp}) if resp else None})
    observed_calls = {r['call_id'] for r in rows}
    request_counts = group(rows, lambda r:r['role'])
    result = {'totals': aggregate(rows), 'event_counts': dict(event_counts), 'duplicate_events_ignored': duplicates,
            'schedule_source': schedule_source, 'method': method, 'planned_budget': evaluation_budget(schedule, method=method, config=config),
            'observed_model_budget': observed_model_budget(root, config),
            'logical_request_events': len(logical_requests),
            'logical_requests_without_recorded_attempt': sorted(set(logical_requests) - observed_calls),
            'logical_responses': len(logical_responses), 'logical_exceptions': len(logical_errors),
            'classification_issues': [{'attempt_id':r['attempt_id'], 'issues':r['classification_issues']}
                                      for r in rows if r['classification_issues']],
            'by_role': request_counts,
            'by_role_and_mc': group(rows, lambda r:r['category']),
            'by_phase': group(rows, lambda r:r['observed_phase']),
            'by_phase_and_role': group(rows, lambda r: str(r['observed_phase'])+'|'+r['category']),
            'by_stage': group(rows, lambda r:r['stage']),
            'by_phase_role_stage': group(rows, lambda r:'|'.join((r['observed_phase'],r['role'],r['stage']))),
            'by_leader_observed_to_target_phase': group([r for r in rows if r['role']=='leader'],
                        lambda r:str(r['observed_phase'])+' -> '+str(r['decision_target_phase'])+'|'+r['stage']),
            'by_round': group(rows, lambda r:r['round_id']), 'attempts': rows}
    providers={r['provider'] for r in rows if r.get('provider')}
    if len(providers)==1:
        provider=next(iter(providers))
        for summary in [result['totals'],*[v for k in ('by_role','by_role_and_mc','by_phase','by_phase_and_role','by_stage','by_phase_role_stage','by_leader_observed_to_target_phase','by_round') for v in result[k].values()]]:
            summary['user_price_reference']=user_price_reference(summary,provider)
        result['user_price_reference']=result['totals']['user_price_reference']
    result['identity']=run_identity(root)
    initial={}
    for row in sorted(rows,key=lambda r:(r['outer_attempt'],r['transport_attempt'],r['attempt_id'])):
        initial.setdefault(row['call_id'],row)
    result['initial_request_prompt_sizes']=group(list(initial.values()),lambda r:'|'.join((r['observed_phase'],r['role'],r['stage'])))
    # Missing backend records are not charged as free. Their token cost is
    # unknown in addition to the per-attempt unknown counters.
    if result.get('user_price_reference'):
        result['user_price_reference']['logical_requests_without_attempt_usage']=len(result['logical_requests_without_recorded_attempt'])
        if result['logical_requests_without_recorded_attempt']:
            result['user_price_reference']['complete_cost_under_reference_tariff']=False
            result['user_price_reference']['total_usd_if_complete']=None
            for scenario in result['user_price_reference'].get('scenarios',{}).values():
                scenario['total_usd_if_complete']=None
    return result


def run_identity(root):
    path=Path(root)/'provenance/config.json'
    config=json.loads(path.read_text()) if path.is_file() else {}
    return {'method':config.get('method'),
            'comparison_variant':config.get('comparison_variant'),
            'api_profile_id':config.get('api_profile_id'),
            'repeat_id':config.get('repeat_id'),
            'context_delivery_protocol':config.get('context_delivery_protocol'),
            'context_efficiency':config.get('context_efficiency'),
            'source':'saved_provenance_config' if config else 'unknown_no_saved_config'}


def user_price_reference(summary, provider):
    """User-supplied USD/M rates, with completeness independent of thinking split."""
    sums=summary['known_token_sums'];unknown=summary['unknown_attempt_count_by_token_field']
    if provider=='anthropic':
        rates={'uncached_input_tokens':2.,'cache_hit_tokens':.2,'cache_creation_tokens':2.5,'output_tokens_including_thinking':10.}
        complete=all(unknown[k]==0 for k in rates)
        components={k:sums[k]*rate/1e6 for k,rate in rates.items()}
        return {'provider':provider,'currency':'USD','rate_source':'user_supplied_reference_not_invoice',
                'rates_per_million':rates,'known_cost_components_usd':components,
                'known_priced_usage_subtotal_usd':sum(components.values()),
                'total_usd_if_complete':sum(components.values()) if complete else None,
                'complete_cost_under_reference_tariff':complete,
                'unknown_attempts_by_priced_field':{k:unknown[k] for k in rates},
                'cache_savings_forecast':None,
                'scope':'Disjoint ordinary/read/5-minute-write input rates; output includes thinking once. Known subtotal may omit unreported categories or transport usage; no forecast of future cache benefit.'}
    if provider in ('gemini','google','deepseek'):
        complete=unknown['input_tokens']==unknown['output_tokens_including_thinking']==0
        scenarios={'flat_input_reference':(.75,3.75)} if provider in ('gemini','google') else {'peak_full_input_reference':(.30,1.20),'offpeak_full_input_reference':(.15,.60)}
        result={}
        for label,(inp,out) in scenarios.items():
            cost=(sums['input_tokens']*inp+sums['output_tokens_including_thinking']*out)/1e6
            result[label]={'input_per_million':inp,'output_per_million':out,'known_priced_usage_subtotal_usd':cost,
                           'total_usd_if_complete':cost if complete else None}
        return {'provider':provider,'currency':'USD','rate_source':'user_supplied_reference_not_invoice',
                'scenarios':result,'complete_cost_under_reference_tariff':complete,
                'unknown_attempts_by_priced_field':{k:unknown[k] for k in ('input_tokens','output_tokens_including_thinking')},
                'actual_cache_adjusted_invoice_cost_known':False,'cache_savings_forecast':None,
                'scope':('All input charged at the stated flat reference rate; no undocumented Gemini cache rebate.' if provider in ('gemini','google') else
                         'All input, including cache hits, charged at full-input peak/offpeak reference rates. DeepSeek cache tariff is unspecified, so actual cache-adjusted billed cost is unknown.')}
    return {'provider':provider,'complete_cost_under_reference_tariff':False,'scope':'No user-supplied tariff for provider'}


def price_scenario(summary, input_rate, output_rate, cache_rate=None):
    sums = summary['known_token_sums']
    ordinary = (sums['input_tokens']*input_rate + sums['output_tokens_including_thinking']*output_rate)/1e6
    cache_known = all(summary['unknown_attempt_count_by_token_field'][k]==0 for k in
                      ('cache_hit_tokens','cache_miss_tokens','cache_creation_tokens','output_tokens_including_thinking'))
    adjusted = ((sums['cache_miss_tokens']*input_rate + sums['cache_hit_tokens']*cache_rate +
                 sums['output_tokens_including_thinking']*output_rate)/1e6) if cache_rate is not None and cache_known and not sums.get('cache_creation_tokens',0) else None
    return {'currency':'USD', 'per_million': {'input':input_rate, 'output':output_rate,'cache_hit':cache_rate},
            'ordinary_input_tariff_scenario_for_measured_usage': ordinary,
            'ordinary_input_tariff_total_if_complete': ordinary if summary['usage_unknown_attempts']==0 else None,
            'cache_adjusted_scenario': adjusted,
            'complete_usage_observed': summary['usage_unknown_attempts'] == 0,
            'note':'Scenario, not invoice; unknown usage is not free. Thinking is included in output. Cache hits are part of input. No cache rate is inferred. Anthropic cache-write pricing requires a separate tariff; cache-adjusted scenario is omitted when writes occur.'}


def token_text(summary, key):
    value = summary['token_totals'][key]
    return str(value) if value is not None else f"unknown (known {summary['known_token_sums'][key]}; {summary['unknown_attempt_count_by_token_field'][key]} attempts unreported)"


def render_markdown(result):
    lines = ['# API usage and engineering feasibility', '',
             'All recorded backend attempts are counted, including rejected replies and retries. '
             'Unknown usage is not zero. Tables show overlapping views of the same attempts; do not add views together. '
             'No API requests or model evaluations are performed by this report.', '',
             'Input includes cached input once. Output includes reasoning once; reasoning and cache columns are subcategories. '
             'Leader phase means the observed results phase. Its next guidance target is listed separately; terminal review has no next-round execution.', '']
    columns = '| View | Logical calls | Attempts | Extra attempts | Input incl. cache | Output incl. reasoning | Reasoning subset | Cache read | Cache write | Ordinary input | Unknown usage attempts |'
    divider = '| --- | ---: | ---: | ---: | --- | --- | --- | --- | --- | --- | ---: |'
    def summary_row(label, value):
        fields = [token_text(value, key) for key in ('input_tokens','output_tokens_including_thinking','thinking_tokens','cache_hit_tokens','cache_creation_tokens','uncached_input_tokens')]
        return '| ' + ' | '.join([label.replace('|',' / '),str(value['logical_calls']),str(value['backend_attempts']),
                                  str(value['extra_attempts_beyond_one_per_call']),*fields,str(value['usage_unknown_attempts'])]) + ' |'
    for name, run in result['runs'].items():
        lines += ['## ' + name, '', 'Schedule source: `' + run['schedule_source'] + '`.', '', columns, divider,
                  summary_row('Total', run['totals'])]
        for grouping in ('by_phase', 'by_role', 'by_stage', 'by_phase_role_stage','by_leader_observed_to_target_phase'):
            lines.extend(summary_row(grouping + ': ' + label, value) for label,value in run[grouping].items())
        budget = run['planned_budget']
        measured = run.get('observed_model_budget', {})
        lines += ['', f"Planned main work: {budget.get('main_candidate_evaluations')} candidate evaluations; "
                  f"at most {budget.get('center_reference_slots')} downstream reference evaluations "
                  f"({budget.get('total_evaluation_attempts_upper_bound')} combined upper bound; exploration excluded). "
                  f"Planned logical API calls: {budget.get('leader_logical_calls')} Leader + {budget.get('worker_logical_calls')} Worker. "
                  'Center-reference reuse is not a model call. '
                  f"Planned fixed-flux preparations: {budget.get('fixed_flux_preparations_planned')}. "
                  f"Measured evaluation attempts including exploration: {measured.get('candidate_evaluations')}; "
                  f"actual VIC/routing process starts: {measured.get('actual_vic_launches')}/{measured.get('actual_routing_launches')}; "
                  f"new center evaluations/reused references: {measured.get('new_downstream_center_evaluations')}/{measured.get('center_reference_reuses')}.", '',
                  '| Round | Phase | Main evals | Maximum center slots | Leader calls planned | Worker calls planned | Actual logical calls | Actual attempts |',
                  '| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
        for row in budget['rounds']:
            actual = run['by_round'].get(str(row['round_id']), {})
            lines.append('| ' + ' | '.join(str(x) for x in (row['round_id'],row['phase'],row['candidate_evaluations'],
                    row['center_reference_slots'],row['expected_leader_logical_calls'],row['expected_worker_logical_calls'],
                    actual.get('logical_calls',0),actual.get('backend_attempts',0))) + ' |')
        if run['logical_requests_without_recorded_attempt']:
            lines += ['', 'Unmeasured logical requests without any recorded backend attempt: ' + str(len(run['logical_requests_without_recorded_attempt'])) + '.']
        if run['classification_issues']:
            lines += ['', 'Classification issues requiring review: ' + str(len(run['classification_issues'])) + '.']
        if 'price_scenario' in run:
            lines += ['', 'Price scenario (user-supplied rates; not an invoice): `' + json.dumps(run['price_scenario']) + '`.']
        if run.get('user_price_reference'):
            lines += ['', 'User price reference (all measured attempts; USD, not an invoice):', '',
                      '```json',json.dumps(run['user_price_reference'],ensure_ascii=False,indent=2),'```']
        lines += ['', 'Cache field observations: `' + json.dumps(run['totals']['cache_observation_counts']) + '`.',
                  f"Known API invocation duration: {run['totals']['known_api_duration_sec']} seconds; "
                  f"{run['totals']['duration_unknown_attempts']} attempts lack duration. This is a sum across attempts, not elapsed run time."]
        lines += ['']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work', type=Path, help='Prepared work root, runs directory, one run directory, or attempt JSONL (historical per-round logs supported)')
    parser.add_argument('--output', type=Path, help='Explicit new summary path; existing file is not overwritten')
    parser.add_argument('--markdown-output', type=Path, help='Human-readable report; defaults to OUTPUT with .md suffix')
    parser.add_argument('--input-price', type=float)
    parser.add_argument('--output-price', type=float)
    parser.add_argument('--cache-hit-price', type=float)
    args = parser.parse_args(argv)
    if (args.input_price is None) != (args.output_price is None):
        parser.error('Provide both input and output rates, or neither')
    if args.cache_hit_price is not None and args.input_price is None:
        parser.error('Cache rate requires input/output rates')
    if any(v is not None and number(v) is None for v in (args.input_price, args.output_price, args.cache_hit_price)):
        parser.error('Rates must be finite and nonnegative')
    def run_sources(directory):
        direct=directory/'llm_calls.jsonl'
        if direct.is_file():return [direct]
        return sorted(directory.glob('round_*/process_trace/llm_attempts.jsonl'))
    if args.work.is_file():
        sources={args.work.parent.name:[args.work]}
    elif run_sources(args.work):
        sources={args.work.name:run_sources(args.work)}
    else:
        base=args.work/'runs' if (args.work/'runs').is_dir() else args.work
        sources={p.name:run_sources(p) for p in sorted(base.iterdir()) if p.is_dir() and run_sources(p)}
    if not sources:parser.error('No actual run API attempt logs found')
    result={'protocol':'native_provider_attempt_usage_audit_v4',
              'scope':'One backend attempt counted once, including failed responses. token_totals is null if any relevant usage is unknown; known_token_sums is only the observed subtotal. Unknown usage is not zero. No API calls.',
              'cache_interpretation':'Cached input is included once. Anthropic full input adds its disjoint uncached/read/write fields. DeepSeek/Gemini prompt totals already include cached input. Output totals include reasoning once; absent reasoning splits remain unknown.',
              'mc_classification':'Saved run schedule; without saved schedule, classification remains unknown. Each Worker candidate is one logical call. Leader phase is observed phase; its guidance target is reported separately.',
              'runs':{name:audit_file(paths) for name,paths in sources.items()}}
    if args.input_price is not None:
        providers={a['provider'] for run in result['runs'].values() for a in run['attempts'] if a.get('provider')}
        if len(providers)>1:
            parser.error('A single tariff cannot price mixed providers. Run this tool on one provider/run at a time, or omit price flags for the whole batch.')
    for name, run in result['runs'].items():
        run['arm']=run['identity']['comparison_variant']
        run['api_profile_id']=run['identity']['api_profile_id']
        if args.input_price is not None:
            for summary in [run['totals'], *[v for key in ('by_phase','by_role','by_stage','by_phase_role_stage') for v in run[key].values()]]:
                summary['price_scenario'] = price_scenario(summary,args.input_price,args.output_price,args.cache_hit_price)
            run['price_scenario'] = run['totals']['price_scenario']
            if run['logical_requests_without_recorded_attempt']:
                run['price_scenario']['ordinary_input_tariff_total_if_complete']=None
                run['price_scenario']['cache_adjusted_scenario']=None
                run['price_scenario']['complete_usage_observed']=False
                run['price_scenario']['logical_requests_without_attempt_usage']=len(run['logical_requests_without_recorded_attempt'])
    markdown_path = args.markdown_output or (args.output.with_suffix('.md') if args.output else None)
    destinations = [p for p in (args.output, markdown_path) if p is not None]
    if len(set(destinations)) != len(destinations) or any(p.exists() for p in destinations):
        parser.error('Report destinations must be distinct new paths; existing reports are never overwritten')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    report = render_markdown(result)
    if markdown_path:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        with markdown_path.open('x', encoding='utf-8') as handle:
            handle.write(report)
    print(report)
    return result


if __name__ == '__main__':
    main()
