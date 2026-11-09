"""Explicit run-local policy for exhausted API/response failures.

A retry that later returns valid output is not a failed logical call.
"""
from pathlib import Path
import json
import math
import threading

_LOCK = threading.Lock()


def validate_policy(policy):
    policy = dict(policy or {})
    if policy.get('mode', 'fallback') not in ('fallback', 'historical_fallback'):
        raise ValueError('llm_failure_policy.mode must be fallback or historical_fallback')
    if 'max_attempts' in policy and (type(policy['max_attempts']) is not int or policy['max_attempts'] < 1):
        raise ValueError('llm_failure_policy.max_attempts must be a positive integer')
    for key in ('timeout_sec', 'retry_delay_sec'):
        if key in policy and (not isinstance(policy[key], (int, float)) or isinstance(policy[key], bool)
                              or not math.isfinite(policy[key]) or policy[key] < 0
                              or (key == 'timeout_sec' and policy[key] == 0)):
            raise ValueError('Invalid llm_failure_policy.' + key)
    return policy


def is_historical_fallback(owner):
    return getattr(owner, 'llm_failure_policy', {}).get('mode') == 'historical_fallback'


def reported_usage_totals(event):
    """Provider-reported aggregates; missing components stay unknown.

    Match the raw-usage ledger's input/output semantics, never compatibility
    adapter zero defaults. Anthropic input includes disjoint ordinary, cache
    read and cache-write categories; known output may survive unknown input.
    """
    unknown = dict.fromkeys(('prompt', 'completion', 'total'))
    u = event.get('usage_provider_reported')
    if not isinstance(u, dict):
        return unknown
    def number(value):
        return value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                         and math.isfinite(value) and value >= 0) else None
    provider = event.get('provider')
    if provider in ('anthropic', 'minimax'):
        ordinary, out = number(u.get('input_tokens')), number(u.get('output_tokens'))
        if ordinary is None or out is None:
            return unknown
        read, write = number(u.get('cache_read_input_tokens')), number(u.get('cache_creation_input_tokens'))
        inp = ordinary + read + write if read is not None and write is not None else None
    elif provider in ('gemini', 'google'):
        inp, visible = number(u.get('prompt_token_count')), number(u.get('candidates_token_count'))
        thinking, total = number(u.get('thoughts_token_count')), number(u.get('total_token_count'))
        if inp is None or visible is None:
            return unknown
        tool = number(u.get('tool_use_prompt_token_count')) or 0
        out = visible + thinking if thinking is not None else (total - inp if total is not None and not tool else None)
        if out is None or out < visible:
            return unknown
    elif provider in ('deepseek', 'openai', 'kimi', 'qwen'):
        inp, out = number(u.get('prompt_tokens')), number(u.get('completion_tokens'))
        if inp is None or out is None:
            return unknown
    else:
        return unknown
    return {'prompt': inp, 'completion': out, 'total': inp + out if inp is not None else None}

def call_usage(owner, response=None):
    """Known subtotal for arithmetic, plus explicit unknown-attempt accounting.

    A zero known subtotal is never a claim that a timed-out call was free.
    Per-attempt logs remain authoritative; this never modifies raw response usage.
    """
    llm = getattr(owner, 'llm', None)
    summary = getattr(llm, 'last_usage_summary', None)
    if isinstance(summary, dict):
        known = summary.get('known_usage', {})
        return {**{k: known.get(k, 0) for k in ('prompt', 'completion', 'total')},
                'unknown_usage_attempts': summary.get('unknown_usage_attempts', 0)}
    if response is not None:
        from .process_trace import response_evidence
        usage = reported_usage_totals(response_evidence(response))
        return {**{k: value if value is not None else 0 for k, value in usage.items()},
                'unknown_usage_attempts': int(any(value is None for value in usage.values()))}
    return {'prompt': 0, 'completion': 0, 'total': 0,
            'unknown_usage_attempts': len(getattr(llm, 'last_attempt_ids', []))}


def record_historical_recovery(owner, reason, *, round_id=None, phase=None, error=None):
    """Record an exhausted logical call without creating a terminal run marker."""
    if not is_historical_fallback(owner):
        return None
    from .process_trace import append_event
    llm = getattr(owner, 'llm', None)
    failure_class = getattr(error, 'failure_class', None) or getattr(llm, 'last_failure_class', None)
    record = {'status': 'historical_fallback_selected',
              'recovery_version': 2, 'recovery_scope': 'logical_call',
              'role': getattr(owner, '_failure_role', 'unknown'),
              'agent_id': getattr(owner, 'agent_id', None),
              'subbasin_id': getattr(owner, 'subbasin_id', None),
              'round_id': round_id, 'phase': phase, 'reason': reason,
              'error_type': type(error).__name__ if error is not None else None,
              'failure_class': failure_class or ('unavailable_client' if llm is None else 'postprocessing'),
              'call_id': getattr(llm, 'last_call_id', None),
              'attempt_ids': list(getattr(llm, 'last_attempt_ids', [])),
              'usage_summary': getattr(llm, 'last_usage_summary', None),
              'fallback_source': 'historical_rule_fallback',
              'next_scheduled_call': 'retry_LLM_normally',
              'policy': dict(owner.llm_failure_policy)}
    append_event(Path(owner._failure_output_root) / 'llm_recoveries.jsonl', record)
    return record

def bind_policy(owner, policy, output_root, role):
    owner.llm_failure_policy = validate_policy(policy)
    owner._failure_output_root = str(output_root)
    owner._failure_role = role
    if owner.llm_failure_policy.get('mode') != 'historical_fallback' or getattr(owner, 'llm', None) is None:
        return
    client = getattr(owner.llm, '_client', owner.llm)
    # Bind role independently of model-provided fields for the narrow null adapter.
    client.response_role = role
    from .output_contract import validate_output
    def validator(data):
        validate_output(data, role, station_ids=getattr(owner, 'worker_ids', ()),
                        subbasin_id=getattr(owner, 'subbasin_id', None))
    client.response_validator = validator


def summarize_attempt_usage(output_root):
    """Audit all actual attempts once, including failed/retried responses.

    Per-call summaries are intentionally not summed again, avoiding duplicate
    billing. A transport exception without a reported usage stays unknown.
    """
    path = Path(output_root) / 'llm_calls.jsonl'
    requested, responses = set(), {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            if row.get('event') == 'attempt_request':
                requested.add(row['attempt_id'])
            elif row.get('event') == 'attempt_response':
                if row['attempt_id'] in responses:
                    raise ValueError('Duplicate API attempt response in usage ledger')
                responses[row['attempt_id']] = row
    known = {'prompt': 0, 'completion': 0, 'total': 0}
    unknown = []
    for aid in sorted(requested):
        row = responses.get(aid, {})
        u = reported_usage_totals(row)
        for key in known:
            if u[key] is not None:
                known[key] += u[key]
        if any(u[key] is None for key in known):
            unknown.append(aid)
    return {'source': 'llm_calls.jsonl attempt_response; each attempt counted once',
            'known_usage': known, 'attempts': len(requested),
            'unknown_usage_attempts': len(unknown), 'unknown_usage_attempt_ids': unknown,
            'usage_complete': not unknown,
            'interpretation': 'Known subtotal is not complete cost when any attempt usage is unknown; '
                              'transport failures are never assumed free.'}
