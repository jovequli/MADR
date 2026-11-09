"""Context-efficiency hook between the refined blocks and the final prompt.

Both options must be false; the blocks pass through unchanged and an audit record
is returned. Ablation adapters may wrap ``project``.
"""
from __future__ import annotations

import copy

KEYS = ('deduplicate_feedback_records', 'lossless_history')


def validate_options(value=None):
    if value is None:
        return dict.fromkeys(KEYS, False)
    if not isinstance(value, dict) or set(value) != set(KEYS) or any(value[k] is not False for k in KEYS):
        raise ValueError('context_efficiency requires both options set to false: ' + ', '.join(KEYS))
    return dict(value)


def project(agent, blocks):
    """Return a copy of the blocks and the audit record for the prompt metadata."""
    options = validate_options(getattr(agent, 'factual_runtime_config', {}).get('context_efficiency'))
    audit = {'protocol': 'lossless_context_efficiency_v1', 'options': options,
             'removed_feedback_fields': [], 'tabulated_records': [], 'history_rows_referenced': 0,
             'input_block_characters_saved': 0}
    return copy.deepcopy(blocks), audit
