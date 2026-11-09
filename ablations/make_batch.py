"""Generate independent launch configurations; never invoke models or providers."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import shutil
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
ROOT = REPOSITORY / 'MADR'
sys.path[:0] = [str(REPOSITORY), str(ROOT), str(ROOT / 'tools')]
from ablations.arm_registry import (ARM_TO_IMPLEMENTATION, SENSITIVITY_ARMS,
                                      FRESH_EXPLORATION_ONLY, implementation_id,
                                      method_for_arm, budget_mode_for_arm, information_scope_for_arm)
from ablations.runtime import VERSION, CONTROLS, configure
from ablations.pairing import inspect_bundle, load_pairing


def arm_method(arm):
    return method_for_arm(arm)


def include_matching_controls(arms):
    """Add one control per represented method, ahead of each repetition's arms."""
    controls = ['control_' + method.lower() for method in
                sorted({arm_method(arm) for arm in arms})]
    return controls + [arm for arm in arms if arm not in controls]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def create_batch(output, settings, arms, provider, repetitions, seed_start,
                 max_parallel, exploration_source=None, suite_id='madr_ablation',
                 pairing_manifest=None):
    output, settings = Path(output).resolve(), Path(settings).resolve(strict=True)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', suite_id) or suite_id in ('.', '..'):
        raise ValueError('suite_id must be a safe directory identifier')
    allowed = set(ARM_TO_IMPLEMENTATION) | set(SENSITIVITY_ARMS) | set(CONTROLS)
    if not arms or len(set(arms)) != len(arms) or not set(arms) <= allowed:
        raise ValueError('Use unique ablation, sensitivity or SADR arm IDs, or control_madr/control_sadr; S3 has a separate entry')
    if any(type(n) is not int or n < 1 for n in (repetitions, max_parallel)):
        raise ValueError('Repetitions and parallel limit must be positive integers')
    if type(seed_start) is not int or not 0 <= seed_start < 2**32 - repetitions:
        raise ValueError('Use a nonnegative seed range within the NumPy seed limit')
    if provider not in ('ds_default', 'sonnet_cached', 'sonnet_default', 'gemini_default'):
        raise ValueError('Unknown API profile')
    if exploration_source and pairing_manifest:
        raise ValueError('Choose either a pairing manifest or one single-run exploration source')
    if exploration_source and repetitions != 1:
        raise ValueError('For repeated reuse, supply one bundle per repetition with --pairing-manifest')
    common_bundle = inspect_bundle(exploration_source) if exploration_source else None
    pairs = load_pairing(pairing_manifest, provider, repetitions,
                         {arm_method(arm) for arm in arms}) if pairing_manifest else None
    server = json.loads(settings.read_text())
    # Keep settings outside generated run workspaces; resolve relative server paths.
    for key in ('binding_json', 'key_file', 'work_root'):
        if server.get(key):
            p = Path(server[key]).expanduser()
            server[key] = str((settings.parent / p).resolve() if not p.is_absolute() else p.resolve())
    server['work_root'] = str(Path(server['work_root']) / suite_id)
    # Resolve and validate every configuration before creating the output directory.
    runs, matrix, planned = [], [], []
    copies = {}
    for rep in range(1, repetitions + 1):
        seed = seed_start + rep - 1
        for arm in arms:
            method = arm_method(arm)
            team = json.loads((ROOT / 'configs' / ('SADR_90round.json' if method == 'SADR' else 'MADR_90round.json')).read_text())
            name = f'{suite_id}_{arm}_{provider}_rep{rep}'
            pair = pairs[method][rep] if pairs else None
            bundle = pair or common_bundle
            main_seed = pair['main_seed'] if pair else seed
            reusable = bundle is not None and implementation_id(arm) not in FRESH_EXPLORATION_ONLY
            team['random_seed'] = main_seed
            team['initial_exploration']['seed'] = bundle['seed'] if bundle else seed
            team['run_name'] = name
            team['output_root'] = 'outputs/' + name
            protocol = {'version': VERSION, 'arm': arm,
                        'budget_mode': budget_mode_for_arm(arm)}
            effective = configure(copy.deepcopy(team), protocol)
            team_file = name + '_config.json'
            launch_file = name + '.json'
            bundle_relative = f'exploration/{method}_rep{rep}.json' if reusable else None
            if reusable:
                copies[bundle_relative] = bundle
            launch = {'schema_version': 1, 'settings_file': 'settings.json',
                      'team_config': team_file, 'run_name': name, 'api_profile': provider,
                      'exploration': {'mode': 'reuse', 'source': bundle_relative} if reusable else {'mode': 'fresh'},
                      'ablation': protocol}
            planned.extend(((team_file, team), (launch_file, launch)))
            runs.append({'config': launch_file, 'lane': 'deepseek_direct' if provider == 'ds_default' else 'proxy'})
            matrix.append({'arm_label': arm, 'method': method, 'repetition': rep,
                           'protocol_version': VERSION,
                           'implementation_id': implementation_id(arm),
                           'row_role': 'control' if arm in CONTROLS else 'ablation',
                           'main_random_seed': main_seed, 'exploration_seed': team['initial_exploration']['seed'],
                           'rounds_per_station': effective['max_rounds'], 'run_name': name,
                           'exploration': 'disabled' if not effective['initial_exploration']['enabled'] else 'reuse' if reusable else 'fresh',
                           'reference_source_run_id': pair['reference_run_id'] if pair else None,
                           'reference_method': method,
                           'reference_bundle_sha256': bundle['sha256'] if bundle else None,
                           'reused_bundle': bundle_relative,
                           'comparison_control_run_id': (
                               f'{suite_id}_control_{method.lower()}_{provider}_rep{rep}'
                               if 'control_' + method.lower() in arms else None),
                           'comparison_control_status': ('same_batch_to_run' if
                               'control_' + method.lower() in arms else 'not_generated'),
                           'intervention_revision': VERSION,
                           'information_scope': information_scope_for_arm(arm)})
    output.mkdir(parents=True, exist_ok=False)
    write(output / 'settings.json', server)
    for relative, bundle in copies.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle['path'], target)
        if inspect_bundle(target)['sha256'] != bundle['sha256']:
            raise ValueError('Exploration bundle changed during batch creation')
    for name, value in planned:
        write(output / name, value)
    if pairing_manifest:
        write(output / 'pairing_source.json', json.loads(Path(pairing_manifest).read_text()))
    lane = 'deepseek_direct' if provider == 'ds_default' else 'proxy'
    write(output / 'suite.json', {'schema_version': 1, 'suite_id': suite_id,
          'max_parallel': max_parallel, 'lane_limits': {lane: max_parallel}, 'runs': runs})
    write(output / 'run_matrix.json', matrix)
    write(output / 'comparison_plan.json', {
        'schema': 'madr-ablation-comparisons-v1', 'protocol_version': VERSION,
        'historical_reference_role': 'exploration_and_seed_provenance_only',
        'historical_outcomes_imported': False,
        'controls': [row for row in matrix if row['row_role'] == 'control'],
        'comparisons': [{'run_name': row['run_name'],
                         'control_run_name': row['comparison_control_run_id'],
                         'method': row['method'], 'repetition': row['repetition'],
                         'exploration_relation': ('same_bundle' if row['exploration'] == 'reuse'
                            else 'omitted_by_design' if row['exploration'] == 'disabled'
                            else 'fresh_by_design')}
                        for row in matrix if row['row_role'] == 'ablation']})
    return {'suite': str(output / 'suite.json'), 'runs': len(runs), 'api_or_model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New configuration directory')
    parser.add_argument('--settings', default=str(ROOT / 'configs/settings.json'))
    parser.add_argument('--arms', nargs='+', required=True, help='Arm IDs, controls, or all-internal')
    parser.add_argument('--provider', choices=('ds_default','sonnet_cached','sonnet_default','gemini_default'), default='sonnet_cached')
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--seed-start', type=int, default=2026100511)
    parser.add_argument('--max-parallel', type=int, default=8)
    parser.add_argument('--exploration-source', help='Complete standard bundle; single-repetition use only')
    parser.add_argument('--pairing-manifest', help='Per-method, per-repetition reference seeds and bundles')
    parser.add_argument('--include-controls', action='store_true', help='Explicitly add matching reference runs')
    parser.add_argument('--suite-id', default='madr_ablation')
    args = parser.parse_args()
    arms = (list(ARM_TO_IMPLEMENTATION) + sorted(SENSITIVITY_ARMS,
            key=lambda arm: int(arm[1:]))
            if args.arms == ['all-internal'] else args.arms)
    try:
        if args.include_controls:
            arms = include_matching_controls(arms)
        result = create_batch(args.output, args.settings, arms, args.provider, args.repetitions,
                              args.seed_start, args.max_parallel, args.exploration_source, args.suite_id,
                              args.pairing_manifest)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
