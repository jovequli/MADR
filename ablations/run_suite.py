"""Run an explicitly generated internal-ablation batch through the shared deployment path."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time

sys.dont_write_bytecode = True
PACKAGE = Path(__file__).resolve().parents[1]
REPOSITORY = PACKAGE
sys.path[:0] = [str(REPOSITORY), str(REPOSITORY / 'MADR'), str(REPOSITORY / 'MADR/tools')]
from ablations import run_ablation as ablation_entry
ablation_entry.configure_deployment()
core = ablation_entry.deployment

PROFILES = {'ds_default': 'deepseek_direct', 'sonnet_cached': 'proxy', 'sonnet_default': 'proxy', 'gemini_default': 'proxy'}


def load_suite(path):
    source = Path(path).expanduser().resolve(strict=True)
    suite = core.read_json(source)
    required = {'schema_version', 'suite_id', 'max_parallel', 'lane_limits', 'runs'}
    if set(suite) != required or suite['schema_version'] != 1:
        raise ValueError('Unsupported suite schema or fields')
    limit = suite['max_parallel']
    if type(limit) is not int or limit < 1:
        raise ValueError('max_parallel must be a positive integer')
    lanes = suite['lane_limits']
    if not isinstance(lanes, dict) or not lanes or any(k not in {'deepseek_direct','proxy'}
            or type(v) is not int or v < 1 for k,v in lanes.items()):
        raise ValueError('Use positive lane limits for deepseek_direct and/or proxy')
    if not isinstance(suite['runs'], list) or not suite['runs']:
        raise ValueError('Choose at least one internal-ablation run')
    specs = []
    for row in suite['runs']:
        if set(row) != {'config', 'lane'} or row['lane'] not in lanes:
            raise ValueError('Each run requires a config and a configured lane')
        spec = core.load_spec(source.parent / row['config'])
        profile = spec['launch']['api_profile']
        if PROFILES.get(profile) != row['lane']:
            raise ValueError('Provider and network lane disagree')
        if spec['config']['max_parallel_workers'] != 1:
            raise ValueError('Keep within-run workers serial')
        spec['lane'] = row['lane']
        specs.append(spec)
    count = len(specs)
    if len({s['work'] for s in specs}) != count or len({s['config']['run_name'] for s in specs}) != count:
        raise ValueError('Every independent run requires its own name and workspace')
    parents = {s['work'].parent for s in specs}
    settings = {s['settings_source'] for s in specs}
    if len(parents) != 1 or len(settings) != 1 or None in settings:
        raise ValueError('Use one external input/settings declaration for this suite')
    parent = next(iter(parents))
    if core.overlaps(parent, PACKAGE):
        raise ValueError('Store results outside the code package')
    return source, suite, specs, parent


def overview(source, suite, specs, parent):
    return {'suite_id': suite['suite_id'], 'suite_source': str(source), 'work_root': str(parent),
            'max_parallel': suite['max_parallel'], 'lane_limits': suite['lane_limits'],
            'runs': [{'run_name': s['config']['run_name'], 'method': s['config']['method'],
                      'api_profile': s['launch']['api_profile'], 'lane': s['lane'],
                      'seed': s['config']['random_seed'], 'work_root': str(s['work']),
                      'rounds_per_station': s['config']['max_rounds'], 'ablation': s['config']['ablation_protocol'],
                      'exploration': s['launch']['exploration'], 'budget': s['budget']} for s in specs]}


def ensure_all_prepared(specs):
    """Prepare all independent run inputs before any child can execute exploration or APIs."""
    manifests = []
    for spec in specs:
        if not spec['work'].exists():
            core.prepare(spec['source'])
        elif not (spec['work'] / 'standalone_manifest.json').is_file():
            raise ValueError('Incomplete existing work directory; keep it for inspection and choose a new settings.work_root')
        manifests.append(core.verify_prepared(spec))
    return manifests


def preflight_all(specs, manifests, parent):
    """Check credentials, routing and executable format without requests or model runs."""
    environments, networks = {}, {}
    for spec in specs:
        if (spec['work'] / 'launcher_receipt.json').exists() or (spec['work'] / 'runs' / spec['config']['run_name']).exists():
            raise ValueError('Existing results are never resumed or overwritten; use a new settings.work_root')
        key_file = spec['launch'].get('key_file')
        if key_file:
            key_file = core.resolve_config_path(key_file, spec['source'])
        env = core.credential_environment(key_file, forbidden_roots=(PACKAGE, parent))
        child, network = core.child_environment(spec['network_policy'], env)
        if not child.get(spec['config']['leader']['api_key_env']):
            raise ValueError('Missing provider key environment variable: ' + spec['config']['leader']['api_key_env'])
        environments[spec['config']['run_name']] = child
        networks[spec['config']['run_name']] = network
    if core.platform.system() != 'Linux':
        raise ValueError('Real runs require Linux; validation does not launch models')
    from preflight_linux import dependencies, inspect_native
    runtime = dependencies()
    binaries = set()
    for spec, manifest in zip(specs, manifests):
        config = core.read_json(spec['work'] / manifest['config_relative'])
        binaries.update(w[k] for w in config['workers'] for k in ('vic_exe', 'rout_exe'))
    native = [inspect_native(str(path)) for path in sorted(binaries)]
    return environments, {'api_or_model_calls': 0, 'runtime': runtime, 'native': native, 'networks': networks}


def run_queue(rows, start, done, *, max_parallel=9, lane_limits=None, pause=time.sleep):
    """A failed launch or child does not cancel independent peers."""
    lane_limits = lane_limits or {'deepseek_direct': 3, 'proxy': 6}
    pending, active, results = list(rows), [], []
    try:
        while pending or active:
            for row in list(pending):
                if len(active) >= max_parallel:
                    break
                if sum(r['lane'] == row['lane'] for r, _ in active) >= lane_limits[row['lane']]:
                    continue
                pending.remove(row)
                try:
                    process = start(row)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    results.append(done(row, None, 127, type(error).__name__))
                else:
                    active.append((row, process))
            changed = False
            for row, process in list(active):
                code = process.poll()
                if code is not None:
                    results.append(done(row, process, code, None))
                    active.remove((row, process))
                    changed = True
            if active and not changed:
                pause(.25)
    except BaseException:
        for _, process in active:
            if process.poll() is None:
                core.stop_group(process)
        raise
    return results


def status(specs, parent):
    rows = []
    for spec in specs:
        name = spec['config']['run_name']
        output = spec['work'] / 'runs' / name
        receipt_path = spec['work'] / 'launcher_receipt.json'
        receipt = core.read_json(receipt_path) if receipt_path.is_file() else {}
        complete = []
        for path in output.glob('round_*/round_meta.json'):
            try:
                meta = core.read_json(path)
                complete.append((meta['round_id'], meta.get('phase')))
            except (OSError, ValueError, KeyError):
                pass
        latest = max(complete, default=(0, None))
        station_progress = []
        sequence_path = output / 'sequential_manifest.json'
        if sequence_path.is_file():
            sequence = core.read_json(sequence_path)
            for station_dir in sorted((output / 'stations').glob('*')):
                local = []
                for path in station_dir.glob('round_*/round_meta.json'):
                    try:
                        meta = core.read_json(path)
                        local.append((meta['round_id'], meta.get('phase')))
                    except (OSError, ValueError, KeyError):
                        pass
                step = max(local, default=(0, None))
                station_progress.append({'station':station_dir.name, 'local_round':step[0],
                    'phase':step[1], 'frozen':station_dir.name in sequence.get('completed_stations', [])})
            latest = (sum(row['local_round'] for row in station_progress), 'station_sequence')
        summary_path = output / 'final_summary.json'
        summary = core.read_json(summary_path) if summary_path.is_file() else {}
        rows.append({'run_name': name, 'status': receipt.get('status', 'prepared' if (spec['work'] / 'standalone_manifest.json').exists() else 'not_prepared'),
                     'latest_complete_round': latest[0], 'phase': latest[1],
                     'best_mean_nse': summary.get('best_mean_nse'),
                     'final_confirmation_mean_nse': summary.get('final_confirmation_mean_nse'),
                     'station_progress': station_progress,
                     'exit_code': receipt.get('exit_code')})
    return {'work_root': str(parent), 'runs': rows, 'api_or_model_calls': 0}


def summarize_all(specs):
    rows = []
    for spec in specs:
        try:
            result = core.summarize(spec['source'])
            rows.append({'run_name': spec['config']['run_name'], 'summary': result})
        except (OSError, ValueError, KeyError, TypeError) as error:
            rows.append({'run_name': spec['config']['run_name'], 'summary_unavailable': type(error).__name__})
    return {'runs': rows, 'api_or_model_calls': 0}


def pack_results(source, suite, specs, parent, archive_path=None):
    control = parent / 'suite_control'
    receipt = core.read_json(control / 'suite_receipt.json')
    if receipt.get('status') not in ('completed', 'completed_with_failures', 'interrupted_or_launcher_error'):
        raise ValueError('Stop or finish this suite before packaging changing result files')
    for spec in specs:
        p = spec['work'] / 'launcher_receipt.json'
        if p.exists() and core.read_json(p).get('status') == 'running':
            raise ValueError('A child still reports running; inspect and finish it before packaging')
    summarize_all(specs)
    archive = Path(archive_path).expanduser().resolve() if archive_path else parent.parent / (suite['suite_id'] + '_results_' + time.strftime('%Y%m%d') + '.tar.gz')
    if core.overlaps(archive, parent) or archive.is_relative_to(PACKAGE):
        raise ValueError('Write the result archive outside the package and run work root')
    files = []
    def add_tree(path, prefix):
        if not path.exists():
            return
        candidates = []
        if path.is_dir():
            for directory, subdirs, filenames in os.walk(path, followlinks=False):
                subdirs[:] = sorted(d for d in subdirs if d != '__pycache__'
                                    and not d.endswith('_calibration_ai')
                                    and not (Path(directory) / d).is_symlink())
                candidates.extend(Path(directory) / name for name in sorted(filenames))
        else:
            candidates = [path]
        for item in candidates:
            if item.is_symlink() or not item.is_file() or '__pycache__' in item.parts or item.name.startswith('._'):
                continue
            relative = item.relative_to(path) if path.is_dir() else Path(item.name)
            if 'routing_cache' in relative.parts and item.suffix != '.json':
                continue  # Preserve cache manifests, not reconstructible flux payloads.
            files.append((item, str(Path(prefix) / relative)))
    top = suite['suite_id'] + '_results'
    add_tree(control, top + '/suite_control')
    # A suite may live beside credentials or large inputs. Include only named,
    # validated configuration files, never its surrounding directory.
    add_tree(source, top + '/requested_configs')
    for spec in specs:
        prefix = top + '/requested_configs/' + spec['config']['run_name']
        add_tree(spec['source'], prefix + '/launch')
        add_tree(spec['team_path'], prefix + '/team')
        if spec['settings_source']:
            add_tree(spec['settings_source'], prefix + '/settings')
    for spec in specs:
        name, work = spec['config']['run_name'], spec['work']
        for folder in ('configs', 'source_snapshot_used', 'logs', 'reports', 'runs'):
            add_tree(work / folder, top + '/' + name + '/' + folder)
        for filename in ('requested_launch.json', 'standalone_manifest.json', 'standalone_preflight.json', 'launcher_receipt.json'):
            add_tree(work / filename, top + '/' + name)
    # Whitelisted evidence only: never input aliases, forcing, model binaries,
    # mutable native workspaces, external keys, virtual environments, or caches.
    with archive.open('xb') as stream, tarfile.open(fileobj=stream, mode='w:gz', compresslevel=6) as tar:
        for path, arcname in files:
            tar.add(path, arcname=arcname, recursive=False)
    digest = core.sha256(archive)
    checksum = archive.with_name(archive.name + '.sha256')
    with checksum.open('x') as stream:
        stream.write(digest + '  ' + archive.name + '\n')
    return {'archive': str(archive), 'sha256': digest, 'files': len(files), 'bytes': archive.stat().st_size,
            'retained': 'All saved run evidence, daily routing, parameters, metrics, LLM traces, audits, source snapshots and logs.',
            'excluded': 'External keys, shared inputs/forcing/binaries, native mutable workspaces, symlinks and virtual environments.'}


def execute(suite_path, action, archive_path=None):
    source, suite, specs, parent = load_suite(suite_path)
    info = overview(source, suite, specs, parent)
    if action == 'validate':
        return {**info, 'api_or_native_calls': 0, 'physical_inputs_checked': False}
    if action == 'status':
        return status(specs, parent)
    if action == 'summarize':
        return summarize_all(specs)
    if action == 'pack':
        return pack_results(source, suite, specs, parent, archive_path)
    directory = parent / 'suite_control'
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / 'suite.lock').open('a')
    logs = {}
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt_path = directory / 'suite_receipt.json'
        if receipt_path.exists():
            raise ValueError('This suite already started; results are never overwritten or automatically resumed')
        manifests = ensure_all_prepared(specs)
        if action == 'prepare':
            return {**info, 'api_or_native_calls': 0, 'prepared_and_verified': len(manifests)}
        environments, preflight = preflight_all(specs, manifests, parent)
        core.write_json(directory / 'suite_preflight.json', preflight)
        receipt = {**info, 'status': 'running', 'started_unix': time.time(),
                   'suite_sha256': core.sha256(source),
                   'settings_sha256': core.sha256(specs[0]['settings_source']),
                   'launcher_sha256': core.sha256(Path(__file__)),
                   'prepared_and_verified_before_launch': len(manifests), 'launched': [], 'completed': []}
        core.write_json(receipt_path, receipt)
        def start(spec):
            name = spec['config']['run_name']
            log = (directory / (name + '.launcher.log')).open('x')
            logs[name] = log
            command = [sys.executable, '-B', str(REPOSITORY / 'ablations/run_ablation.py'), '--config', str(spec['source']), '--action', 'run']
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=environments[name])
            receipt['launched'].append({'run_name': name, 'pid': child.pid, 'lane': spec['lane'], 'started_unix': time.time()})
            core.write_json(receipt_path, receipt)
            return child
        def done(spec, child, code, error_type):
            name = spec['config']['run_name']
            if name in logs:
                logs[name].close()
            row = {'run_name': name, 'exit_code': code, 'ended_unix': time.time(),
                   'run_receipt': str(spec['work'] / 'launcher_receipt.json')}
            if error_type:
                row['launcher_error_type'] = error_type
            receipt['completed'].append(row)
            core.write_json(receipt_path, receipt)
            return row
        try:
            results = run_queue(specs, start, done, max_parallel=suite['max_parallel'], lane_limits=suite['lane_limits'])
            receipt.update(status='completed' if all(r['exit_code'] == 0 for r in results) else 'completed_with_failures', ended_unix=time.time())
            core.write_json(receipt_path, receipt)
            return receipt
        except BaseException as error:
            receipt.update(status='interrupted_or_launcher_error', error_type=type(error).__name__, ended_unix=time.time())
            core.write_json(receipt_path, receipt)
            raise
    finally:
        for log in logs.values():
            log.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', required=True, help='suite.json created by make_batch.py')
    parser.add_argument('--action', choices=('validate', 'prepare', 'run', 'status', 'summarize', 'pack'), default='validate')
    parser.add_argument('--archive-path', help='New output .tar.gz path, outside code and work root')
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Suite interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        result = execute(args.suite, args.action, args.archive_path)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if result.get('status', 'completed') == 'completed' else 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, type(error).__name__ + ': ' + str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
