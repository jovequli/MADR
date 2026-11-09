"""One source for local output validation and compact model-visible field types.

Schema files define physical output shapes. Runtime context owns identities and
fixed scheduling metadata; annotations never manufacture executable actions.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / 'schemas'
PROTOCOL = 'typed_output_contract_v1'


def load_output_schema(role):
    name = str(role).lower()
    if name not in ('leader', 'worker'):
        raise ValueError('Unknown output role')
    return json.loads((SCHEMAS_DIR / (name + '_output.json')).read_text(encoding='utf-8'))


def validate_schema(data, schema, path='$'):
    expected = schema.get('type')
    checks = {'object': lambda x: isinstance(x, dict), 'array': lambda x: isinstance(x, list),
              'number': lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x),
              'integer': lambda x: type(x) is int, 'string': lambda x: isinstance(x, str),
              'boolean': lambda x: type(x) is bool, 'null': lambda x: x is None}
    if expected in checks and not checks[expected](data):
        raise ValueError('JSON schema type mismatch at ' + path + ': ' + expected)
    if 'enum' in schema and data not in schema['enum']:
        raise ValueError('JSON schema enum mismatch at ' + path)
    if schema.get('x-nonblank') and (not isinstance(data, str) or not data.strip()):
        raise ValueError('JSON schema nonblank text required at ' + path)
    if isinstance(data, dict):
        if schema.get('additionalProperties') is False and set(data) - set(schema.get('properties', {})):
            raise ValueError('JSON schema contains unknown field at ' + path)
        missing = [k for k in schema.get('required', []) if k not in data]
        if missing:
            raise ValueError('JSON schema missing required field at ' + path + ': ' + ', '.join(missing))
        for key, spec in schema.get('properties', {}).items():
            if key in data:
                validate_schema(data[key], spec, path + '.' + key)
    if isinstance(data, list) and 'items' in schema:
        for i, item in enumerate(data):
            validate_schema(item, schema['items'], path + '[' + str(i) + ']')


def validate_output(data, role, *, station_ids=(), subbasin_id=None):
    role = str(role).lower()
    validate_schema(data, load_output_schema(role))
    if role == 'leader':
        ids = [g['subbasin_id'] for g in data['worker_guidance']]
        if len(ids) != len(set(ids)) or set(ids) != set(station_ids):
            raise ValueError('Leader guidance must cover each configured station exactly once')
    elif 'subbasin_id' in data and data['subbasin_id'] != subbasin_id:
        raise ValueError('Worker subbasin identity does not match the requested station')


def normalize_runtime_metadata(data, role, context=None):
    """Narrow, fully audited normalization; never repairs parameter names/values."""
    result = copy.deepcopy(data); changes = []; context = context or {}; role = str(role).lower()
    def change(obj, key, path, value=None, remove=False, reason='runtime_owned_metadata'):
        before = copy.deepcopy(obj.get(key))
        existed = key in obj
        if remove:
            if not existed: return
            del obj[key]
        else:
            if existed and obj[key] == value and type(obj[key]) is type(value): return
            obj[key] = value
        changes.append({'path': path, 'operation': 'remove' if remove else 'assign_runtime_value',
                        'present_before': existed, 'before': before, 'after': None if remove else value,
                        'reason': reason})
    if isinstance(result, dict) and role in ('leader', 'worker'):
        # Neither role assigns its candidate/round identity. Preserve supplied
        # values in the audit; a string/invented number cannot redirect execution.
        rid = context.get('round_id')
        if type(rid) is int:
            change(result, 'round_id', '$.round_id', rid)
        else:
            change(result, 'round_id', '$.round_id', remove=True)
        if role == 'leader':
            change(result, 'stop_or_continue', '$.stop_or_continue', 'continue')
            change(result, 'phase_recommendation', '$.phase_recommendation', 'stay')
            for key in ('global_score_trend', 'next_round_strategy', 'stop_reason'):
                if key in result and not isinstance(result[key], str):
                    change(result, key, '$.' + key, remove=True, reason='invalid_optional_nonexecuting_annotation')
            for i, g in enumerate(result.get('worker_guidance', []) if isinstance(result.get('worker_guidance'), list) else []):
                if not isinstance(g, dict): continue
                change(g, 'search_radius_hint', f'$.worker_guidance[{i}].search_radius_hint', 'keep')
                for j, action in enumerate(g.get('param_adjustments', []) if isinstance(g.get('param_adjustments'), list) else []):
                    if isinstance(action, dict) and action.get('coupled_group', object()) is None:
                        change(action, 'coupled_group', f'$.worker_guidance[{i}].param_adjustments[{j}].coupled_group',
                               remove=True, reason='remove_optional_null')
        else:
            for key in ('vic', 'routing'):
                if key in result and result[key] is None:
                    change(result, key, '$.' + key, remove=True, reason='remove_optional_null')
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    audit = {'protocol': PROTOCOL, 'role': role, 'applied': bool(changes), 'changes': changes,
             'parsed_before_sha256': digest(data), 'parsed_after_sha256': digest(result),
             'semantics': 'Runtime-owned metadata and optional nulls only; physical values, station identity and trial hypotheses are never fabricated.'}
    if changes: audit.update(parsed_before=copy.deepcopy(data), parsed_after=copy.deepcopy(result))
    return result, audit


def _shape(schema):
    if 'x-annotation-schema' in schema:
        return 'optional annotation (see trial below)'
    if 'enum' in schema: return '/'.join(str(x) for x in schema['enum'])
    kind = schema.get('type', 'annotation')
    if kind == 'array': return 'array<' + _shape(schema['items']) + '>'
    if kind == 'object':
        required = set(schema.get('required', []))
        return '{' + ', '.join(k + ('' if k in required else '?') + ':' + _shape(v)
                               for k, v in schema.get('properties', {}).items() if not v.get('x-runtime-owned') and not v.get('x-legacy-alias')) + '}'
    return kind + (' (nonblank)' if schema.get('x-nonblank') else '')
