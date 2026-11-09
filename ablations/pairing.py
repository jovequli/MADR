"""Resolve prespecified reference repetitions without modifying source bundles."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def seed(value, label):
    if type(value) is not int or not 0 <= value < 2**32:
        raise ValueError(label + ' must be an integer in the NumPy seed range')
    return value


def inspect_bundle(path):
    from scripts.exploration_reuse import _read_bundle
    path = Path(path).expanduser().resolve(strict=True)
    bundle, _ = _read_bundle(path)
    design = bundle['identity']['design']
    if (design.get('n_near'), design.get('n_random')) != (10, 40):
        raise ValueError('Reference reuse requires the standard 10-near + 40-global exploration')
    return {'path': path, 'seed': seed(design.get('seed'), 'Exploration seed'),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'payload_sha256': bundle['payload_sha256'],
            'protocol_sha256': bundle['protocol_sha256']}


def load_pairing(path, provider, repetitions, methods):
    """Each method owns a distinct reference set, paired by repetition number."""
    source = Path(path).expanduser().resolve(strict=True)
    data = json.loads(source.read_text())
    if data.get('schema') != 'madr-ablation-pairing-v1':
        raise ValueError('Unsupported pairing manifest schema')
    if data.get('provider_profile') != provider:
        raise ValueError('Pairing reference provider profile differs from the requested provider')
    sets = data.get('reference_sets', {})
    result = {}
    for method in sorted(methods):
        entries = sets.get(method)
        if not isinstance(entries, list) or len(entries) != repetitions:
            raise ValueError(f'{method} requires exactly {repetitions} reference entries')
        by_rep = {}
        for item in entries:
            rep = item.get('repetition')
            if type(rep) is not int or not 1 <= rep <= repetitions or rep in by_rep:
                raise ValueError('Pairing repetitions must be unique and cover 1..repetitions')
            name = item.get('reference_run_id')
            if not isinstance(name, str) or not name.strip():
                raise ValueError('Each pairing entry requires its reference_run_id')
            value = item.get('exploration_source')
            if not isinstance(value, str) or not value:
                raise ValueError('Each pairing entry requires its complete exploration source')
            bundle_path = Path(value).expanduser()
            bundle = inspect_bundle(bundle_path if bundle_path.is_absolute() else source.parent / bundle_path)
            if item.get('exploration_seed') != bundle['seed']:
                raise ValueError('Manifest exploration seed disagrees with its actual bundle')
            if item.get('bundle_sha256') != bundle['sha256']:
                raise ValueError('Manifest bundle checksum disagrees with the actual file')
            by_rep[rep] = {**bundle, 'main_seed': seed(item.get('main_random_seed'), 'Main seed'),
                           'reference_run_id': name}
        if len({x['seed'] for x in by_rep.values()}) != repetitions:
            raise ValueError('Use a distinct exploration seed for each reference repetition')
        if len({x['payload_sha256'] for x in by_rep.values()}) != repetitions:
            raise ValueError('Reference repetitions must not reuse the same exploration payload')
        if len({x['main_seed'] for x in by_rep.values()}) != repetitions:
            raise ValueError('Use distinct main seeds across reference repetitions')
        result[method] = by_rep
    return result
