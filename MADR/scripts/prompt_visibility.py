"""Presentation of stored diagnostics in agent prompts.

Static source bytes remain authoritative. The control presentation passes stored
diagnostic and execution objects through unchanged; only the Worker view of
code-owned diagnostic reasoning is limited to 120 characters."""

PROTOCOL = 'minimal_prompt_visibility_v1'
ARMS = ('control',)


def validate_prompt_visibility_protocol(value=None, *, method=None):
    if value is None:
        value = {'protocol': PROTOCOL, 'arm': 'control'}
    if (not isinstance(value, dict) or set(value) != {'protocol', 'arm'}
            or value.get('protocol') != PROTOCOL or value.get('arm') not in ARMS):
        raise ValueError('Invalid prompt_visibility_protocol: require exact protocol and supported arm')
    return dict(value)


def validate_visibility_config(config, *, method):
    return validate_prompt_visibility_protocol(config.get('prompt_visibility_protocol'), method=method)


def worker_reason(reason, protocol=None):
    """Worker view of code-owned diagnostic reasoning, limited to 120 characters."""
    return str(reason)[:120]


def visibility_metadata(protocol=None):
    return {'prompt_visibility_protocol': validate_prompt_visibility_protocol(protocol),
            'visibility_scope': 'model-facing presentation only; stored diagnostic/coordination objects and model reasoning unchanged'}
