"""Additive process records: no reader here participates in calibration feedback."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
import datetime as dt
import json
import logging
import math
from pathlib import Path
import threading
import uuid
from copy import deepcopy
from functools import wraps

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_ERRORS = []
_CONTEXT = ContextVar('madr_llm_trace', default=None)
_SECRET_KEYS = {'api_key', 'access_token', 'refresh_token', 'password', 'secret',
                'authorization', 'cookie', 'cookies', 'credential', 'credentials'}


def new_id():
    return uuid.uuid4().hex


def note_trace_error(path, kind, error, critical=True):
    with _LOCK:
        _ERRORS.append({'path': str(path), 'kind': kind, 'error_type': type(error).__name__,
                        'critical': critical, 'utc': dt.datetime.now(dt.timezone.utc).isoformat()})


def get_trace_errors(output_root=None):
    with _LOCK:
        rows = list(_ERRORS)
    if output_root is not None:
        root = str(Path(output_root).absolute()).rstrip('/') + '/'
        rows = [r for r in rows if str(Path(r['path']).absolute()).startswith(root)]
    return [dict(row) for row in rows]


def capture_snapshot(value, output_path):
    """Detach per-call evidence without allowing an archival failure into control flow."""
    try:
        return deepcopy(value)
    except Exception as error:
        note_trace_error(output_path, 'proposal_snapshot', error)
        return None


def _record_only(function):
    @wraps(function)
    def safe(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as error:
            first = args[0] if args else kwargs.get('round_dir', '')
            if function.__name__ == 'record_worker_proposal':
                first = getattr(first, '_trace_output_dir', '') or ''
            note_trace_error(Path(first) / 'process_trace', function.__name__, error)
            logger.warning('Process evidence could not be prepared (%s)', type(error).__name__)
            return None
    return safe


def snapshot(value):
    """JSON-safe detached copy. Nonfinite diagnostics are unavailable, never zero."""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(k): ('<redacted>' if str(k).lower() in _SECRET_KEYS else snapshot(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [snapshot(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, 'item'):
        return snapshot(value.item())
    if hasattr(value, 'model_dump'):
        return snapshot(value.model_dump())
    return {'unserialized_type': type(value).__name__}


def append_event(path, event, enabled=True):
    """Never let recording failure change retries, candidates or feedback."""
    if not enabled or not path:
        return False
    try:
        path = Path(path)
        row = {'trace_version': 1, 'recorded_utc': dt.datetime.now(dt.timezone.utc).isoformat(), **event}
        encoded = json.dumps(snapshot(row), ensure_ascii=False, allow_nan=False)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(encoded + '\n')
        return True
    except Exception as error:
        note_trace_error(path, 'jsonl_event', error)
        logger.warning('Process record could not be written (%s)', type(error).__name__)
        return False


def write_json(path, data):
    try:
        path = Path(path)
        encoded = json.dumps(snapshot(data), ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded, encoding='utf-8')
        return True
    except Exception as error:
        note_trace_error(path, 'json_snapshot', error)
        logger.warning('Process snapshot could not be written (%s)', type(error).__name__)
        return False


def write_markdown(path, title, data):
    """Render only already-computed data; never solicit new reasoning."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(snapshot(data), ensure_ascii=False, indent=2, allow_nan=False)
        # JSON fenced rendering preserves distinctions between rules and LLM text.
        path.write_text(f'# {title}\n\nRecorded process data; no additional model interpretation.\n\n```json\n{body}\n```\n', encoding='utf-8')
        return True
    except Exception as error:
        note_trace_error(path, 'markdown_render', error, critical=False)
        logger.warning('Process Markdown could not be written (%s)', type(error).__name__)
        return False


@contextmanager
def llm_trace_context(path, call_id, agent_id='', enabled=True, meta=None):
    token = _CONTEXT.set({'path': str(path) if path else None, 'call_id': call_id,
                          'agent_id': agent_id, 'enabled': enabled, 'meta': snapshot(meta or {})})
    try:
        yield _CONTEXT.get()
    finally:
        _CONTEXT.reset(token)


def call_context(client):
    context = _CONTEXT.get()
    if context is None:
        options = getattr(client.config, 'extra_params', {}) or {}
        context = {'path': options.get('process_trace_path'), 'call_id': new_id(),
                   'agent_id': '', 'enabled': options.get('process_trace_enabled', True), 'meta': {}}
    client.last_call_id = context['call_id']
    return context


def emit_attempt(context, event, **values):
    return append_event(context.get('path'), {'event': event, 'call_id': context['call_id'],
                        'agent_id': context.get('agent_id', ''), 'context': context.get('meta', {}),
                        **values}, enabled=context.get('enabled', True))


def response_evidence(response):
    """Read only response/usage attributes, never headers, clients or credentials."""
    raw = getattr(response, 'raw_response', None)
    provider = getattr(response, 'provider', '')
    def field(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    if provider == 'gemini':
        # The response wrapper copies config.model; read the provider-reported model from raw.
        reported = field(raw, 'model_version') or field(raw, 'model') or ''
    else:
        reported = field(raw, 'model') or getattr(response, 'model', '') or ''
    usage = field(raw, 'usage') or field(raw, 'usage_metadata')
    transport_call_id = None
    if isinstance(raw, dict) and 'cli_wrapper' in raw:
        wrapper = raw.get('cli_wrapper') or {}
        usage = wrapper.get('usage')
        transport_call_id = (raw.get('transport_metadata') or {}).get('call_id')
    # Keep provider-returned reasoning separate from the final JSON channel. It is
    # audit evidence only, never input to JSON parsing or hydrologic feedback.
    reasoning_content = getattr(response, 'reasoning_content', None)
    if reasoning_content is None:
        choices = field(raw, 'choices') or []
        if choices:
            reasoning_content = field(field(choices[0], 'message'), 'reasoning_content')
    usage_breakdown = None
    if provider == 'deepseek':
        completion_details = field(usage, 'completion_tokens_details')
        prompt_details = field(usage, 'prompt_tokens_details')
        reasoning_tokens = field(completion_details, 'reasoning_tokens')
        completion_tokens = field(usage, 'completion_tokens')
        cache_hit = field(usage, 'prompt_cache_hit_tokens')
        if cache_hit is None:
            cache_hit = field(prompt_details, 'cached_tokens')
        final_tokens = (completion_tokens - reasoning_tokens
                        if type(completion_tokens) is int and type(reasoning_tokens) is int
                        and 0 <= reasoning_tokens <= completion_tokens else None)
        usage_breakdown = {'prompt_cache_hit_tokens': cache_hit,
                           'prompt_cache_miss_tokens': field(usage, 'prompt_cache_miss_tokens'),
                           'reasoning_tokens': reasoning_tokens,
                           'completion_includes_reasoning': True,
                           'final_content_tokens_derived': final_tokens,
                           'source': 'provider usage; derived final = completion minus reasoning when available'}
    elif provider == 'anthropic':
        # These three provider fields are disjoint input categories. Output
        # includes thinking; its separate token count is not exposed here.
        usage_breakdown = {
            'input_uncached_tokens': field(usage, 'input_tokens'),
            'prompt_cache_hit_tokens': field(usage, 'cache_read_input_tokens'),
            'prompt_cache_creation_tokens': field(usage, 'cache_creation_input_tokens'),
            'reasoning_tokens': None,
            'completion_includes_reasoning': True,
            'final_content_tokens_derived': None,
            'source': 'Anthropic raw usage: total input is uncached plus cache read plus cache creation; reasoning token split unavailable',
        }
    elif provider == 'gemini':
        usage_breakdown = {
            'prompt_cache_hit_tokens': field(usage, 'cached_content_token_count'),
            'reasoning_tokens': field(usage, 'thoughts_token_count'),
            'final_content_tokens': field(usage, 'candidates_token_count'),
            'completion_includes_reasoning': True,
            'source': 'Gemini raw usage: cached input is a subset of prompt; compatibility completion is candidates plus thoughts',
        }
    return {'model_reported': reported, 'provider': provider,
            'provider_response_id': snapshot(field(raw, 'id')),
            'provider_system_fingerprint': snapshot(field(raw, 'system_fingerprint')),
            'content': getattr(response, 'content', None),
            'reasoning_content': reasoning_content,
            'reasoning_content_scope': 'provider-returned auxiliary channel; not parsed as final JSON',
            'usage_breakdown': usage_breakdown,
            'finish_reason': getattr(response, 'finish_reason', None),
            'usage_compatibility': {'prompt': getattr(response, 'prompt_tokens', None),
                                    'completion': getattr(response, 'completion_tokens', None),
                                    'total': getattr(response, 'total_tokens', None)},
            'usage_provider_reported': snapshot(usage),
            'transport_call_id': transport_call_id,
            'record_scope': 'framework backend invocation; SDK/provider internal network retries are not enumerated'}


@_record_only
def record_worker_proposal(worker, trace):
    row = {'event': 'worker_proposal', 'proposal_id': new_id(),
           'subbasin_id': worker.subbasin_id, 'agent_id': worker.agent_id, **trace}
    worker.last_proposal_trace = snapshot(row)
    worker.last_call_id = trace.get('call_id')
    output = getattr(worker, '_trace_output_dir', None)
    if output:
        append_event(Path(output) / 'process_trace.jsonl', row,
                     enabled=getattr(worker, 'process_trace_enabled', True))
    return worker.last_proposal_trace


@_record_only
def trace_worker_candidate(round_dir, round_id, worker, params, metrics, ok,
                           candidate_id=1, label='single', reasoning='', leader_context='',
                           branch='', candidate_before_locks=None, phase=None,
                           fallback_reason=None, proposal_trace=None, **extra):
    """Save true graph variables. Pass a captured proposal_trace for each MC call."""
    row = {'event': 'candidate_process', 'round_id': round_id,
           'subbasin_id': worker.subbasin_id, 'agent_id': worker.agent_id,
           'candidate_id': candidate_id, 'label': label, 'phase': phase,
           'branch': branch, 'candidate_before_locks': candidate_before_locks,
           'effective_params': params, 'ok': ok, 'metrics': metrics,
           'reasoning': reasoning, 'leader_context_received': leader_context,
           'fallback_reason': fallback_reason, 'proposal_trace': proposal_trace, **extra}
    if not getattr(worker, 'process_trace_enabled', True):
        return snapshot(row)
    directory = Path(round_dir) / 'process_trace'
    write_json(directory / f'{worker.subbasin_id}_c{candidate_id}_{label}.json', row)
    append_event(Path(round_dir).parent / 'process_trace.jsonl', row)
    return snapshot(row)


@_record_only
def write_mc_diagnostics(round_dir, round_id, subbasin_id, candidates,
                         previous_metrics=None, context=None, winner_id=None):
    """Pure diagnostic derivation for archiving, never set Worker._last_diagnosis."""
    from .worker_diagnostics import diagnose_for_policy
    for index, candidate in enumerate(candidates, 1):
        cid = candidate.get('candidate_id', candidate.get('idx', index))
        metrics = candidate.get('metrics') or {}
        candidate_context = dict(context or {}, simulation_ok=candidate.get('ok'))
        diagnostic = diagnose_for_policy(candidate_context.get('diagnostic_mode', 'historical_policy'), metrics, previous_metrics, candidate_context)
        row = {'source': 'code_diagnostic', 'round_id': round_id, 'subbasin_id': subbasin_id,
               'candidate_id': cid, 'is_winner': cid == winner_id, 'candidate': candidate,
               'previous_metrics': previous_metrics, 'diagnostic_context': context,
               'diagnosis': snapshot(diagnostic)}
        name = f'worker_{subbasin_id}_c{cid}_diagnostic'
        write_json(Path(round_dir) / 'process_trace' / f'{name}.json', row)
        write_markdown(Path(round_dir) / 'process_trace' / f'{name}.md', name, row)


@_record_only
def write_round_trace(round_dir, round_id, diagnostic_reports, coordination_result,
                      leader_decision, worker_results, objective_weights, phase,
                      score_values=None, **extra):
    """Persist current data and score ingredients without mutating graph state."""
    station_components = []
    for result in worker_results:
        metrics = result.get('metrics') or {}
        ratios = {k: metrics.get(k) for k in ('peak_ratio', 'lowflow_ratio')}
        station_components.append({'subbasin_id': result.get('subbasin_id'),
                                   'ok': result.get('ok'), 'NSE': metrics.get('NSE'),
                                   'ratio_values': ratios,
                                   'absolute_ratio_deviations': {k: abs(v-1) if isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) else None for k, v in ratios.items()},
                                   'evaluation_start': metrics.get('evaluation_start'),
                                   'evaluation_end': metrics.get('evaluation_end'),
                                   'n_scored_days': metrics.get('n_scored_days'),
                                   'diagnostic_status': metrics.get('diagnostic_status')})
    row = {'round_id': round_id, 'phase': phase, 'diagnostic_source': 'deterministic_diagnostic_archive',
           'prompt_exposure_source': 'diagnostics_prompt_block and saved LLM request',
           'archive_is_not_prompt_exposure': True,
           'diagnostic_reports': diagnostic_reports, 'coordination_result': coordination_result,
           'leader_decision_adopted': leader_decision, 'worker_results': worker_results,
           'objective_weights': objective_weights, 'score_values_full_precision': score_values,
           'station_score_components': station_components, **extra}
    directory = Path(round_dir) / 'process_trace'
    write_json(directory / 'round_context.json', row)
    write_json(directory / 'leader_decision_adopted.json', leader_decision)
    write_markdown(directory / 'round_context.md', f'Round {round_id} process trace', row)
    for report in diagnostic_reports:
        sid = report.get('subbasin_id', 'unknown')
        stored = {'source': 'code_diagnostic', 'round_id': round_id, **report}
        write_json(directory / f'worker_{sid}_diagnostic.json', stored)
        write_markdown(directory / f'worker_{sid}_diagnostic.md', f'Round {round_id} {sid} diagnostic', stored)
    return snapshot(row)


@_record_only
def write_existing_mc_diagnostics(round_dir, round_id, workers, worker_results, upstream_map):
    """Derive archival diagnostics with the same context as post_round_diagnostics.

    The MC JSON is read only by this recorder, never returned to graph state or prompts.
    """
    by_sid = {r['subbasin_id']: r for r in worker_results}
    for sid, worker in workers.items():
        if sid not in by_sid or not getattr(worker, 'process_trace_enabled', True):
            continue
        path = Path(round_dir) / 'results' / f'worker_{sid}.json'
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            candidates = saved.get('mc_detail')
            if not candidates:
                continue
            last = worker.ledger.get_last_entry() if hasattr(worker.ledger, 'get_last_entry') else None
            previous = last.get('metrics') if last else None
            quality = 'good'
            for up_sid in upstream_map.get(sid, []):
                nse = by_sid.get(up_sid, {}).get('metrics', {}).get('NSE', 0)
                if isinstance(nse, (int, float)) and nse < 0.5:
                    quality = 'poor'
                    break
            context = {'is_headwater': not upstream_map.get(sid, []), 'upstream_quality': quality}
            if getattr(worker, 'diagnostic_mode', None) == 'historical_policy':
                from .diagnostic_evidence import finite
                upstreams = [by_sid.get(u, {}) for u in upstream_map.get(sid, [])]
                quality = 'unknown'
                if upstreams and all(r.get('ok') is True and finite(r.get('metrics', {}).get('NSE')) is not None for r in upstreams):
                    quality = 'poor' if any(float(r['metrics']['NSE']) < .5 for r in upstreams) else 'good'
                context.update(diagnostic_mode=worker.diagnostic_mode, upstream_quality=quality)
            write_mc_diagnostics(round_dir, round_id, sid, candidates, previous, context,
                                 saved.get('winning_candidate_id'))
        except Exception as error:
            note_trace_error(path, 'mc_diagnostic_source', error)
