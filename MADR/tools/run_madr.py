"""JSON-driven MADR deployment entry.

validate: configuration and budget only. prepare: read/hash/isolate inputs, snapshot
source, and certify optional exploration reuse (zero physical/API calls). run:
prepare if necessary, then execute exactly the frozen team config from its snapshot.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import importlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

from api_credentials import credential_environment
from linux_common import (API_PROFILES, ROOT,
                          configure_provider, overlaps, provider_network_policy,
                          sha256, read_json, write_new)
from prepare_linux import InputPlan, binding_rows
from transport_runtime import child_environment, stop_group

RELEASE = 'MADR_105'
PACKAGE_ROOT = ROOT
FRAMEWORK = '.'
LAUNCH_KEYS = {'schema_version', 'team_config', 'binding_json', 'work_root', 'run_name',
               'api_profile', 'key_file', 'exploration', 'settings_file', '_comment'}


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def resolve_config_path(value, source):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else source.parent / path).resolve()


def no_embedded_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {'api_key', 'authorization', 'api_secret', 'access_token'} and item:
                raise ValueError('Literal credentials are not allowed in JSON; use environment/key_file')
            no_embedded_credentials(item)
    elif isinstance(value, list):
        for item in value:
            no_embedded_credentials(item)


def framework_path(method='MADR'):
    if method not in ('MADR', 'SADR'):
        raise ValueError('method must be MADR or SADR')
    return '.'


def framework_modules(root=ROOT, method='MADR'):
    # Keep the imported scripts package bound to this repository.
    base = str(root / framework_path(method))
    loaded = sys.modules.get('scripts')
    if loaded is not None and str(Path(loaded.__file__).resolve().parent.parent) != base:
        for name in list(sys.modules):
            if name == 'scripts' or name.startswith('scripts.'):
                del sys.modules[name]
    old = str(root / FRAMEWORK)
    while old in sys.path:
        sys.path.remove(old)
    sys.path.insert(0, base)
    return (importlib.import_module('scripts.execution_protocol'),
            importlib.import_module('scripts.run_contract'),
            importlib.import_module('scripts.phase_config'))


def resolve_launch(source):
    """A single server settings JSON supplies paths; each run stays independently usable."""
    launch = read_json(source)
    no_embedded_credentials(launch)
    if set(launch) - LAUNCH_KEYS or launch.get('schema_version') != 1:
        raise ValueError('Unknown standalone launch field or schema_version')
    settings_source = None
    resolved = copy.deepcopy(launch)
    if launch.get('settings_file'):
        settings_source = resolve_config_path(launch['settings_file'], source)
        settings = read_json(settings_source)
        no_embedded_credentials(settings)
        if set(settings) - {'schema_version','binding_json','work_root','key_file','_comment'} or settings.get('schema_version') != 1:
            raise ValueError('Unknown server settings field or schema_version')
        for key in ('binding_json', 'key_file'):
            if key not in launch and settings.get(key):
                resolved[key] = str(resolve_config_path(settings[key], settings_source))
        if 'work_root' not in launch:
            if not isinstance(settings.get('work_root'), str) or not settings['work_root']:
                raise ValueError('Server settings require work_root')
            resolved['work_root'] = str(resolve_config_path(settings['work_root'], settings_source) / launch.get('run_name', ''))
    return resolved, settings_source


def validate_team(config):
    """Validate the executing core and derive its budget from the JSON schedule."""
    no_embedded_credentials(config)
    if config.get('experiment_protocol', {}).get('anti_flip_policy') != 'measured_direction_momentum_v1':
        raise ValueError('This configuration requires anti_flip_policy=measured_direction_momentum_v1')
    method = config.get('method')
    framework_path(method)
    protocol, contract, phases = framework_modules(method=method)
    protocol.validate_periods(config)
    protocol.validate_static_input_protocol(config.get('static_input_protocol'))
    protocol.validate_worker_evidence_mode(config.get('worker_evidence_mode'))
    importlib.import_module('scripts.context_delivery').validate_context_config(config, method=method)
    importlib.import_module('scripts.prompt_visibility').validate_visibility_config(config, method=method)
    importlib.import_module('scripts.llm_failure_policy').validate_policy(config.get('llm_failure_policy'))
    contract.validate_serial(config)
    importlib.import_module('scripts.center_reference_cache').validate_config(config.get('center_reference_cache'))
    evidence = config.get('current_evidence_protocol', {'protocol':'E_current_reference_v1', 'enabled':False})
    if (not isinstance(evidence, dict) or set(evidence) != {'protocol','enabled'}
            or evidence['protocol'] != 'E_current_reference_v1' or type(evidence['enabled']) is not bool):
        raise ValueError('Invalid current_evidence_protocol; use explicit protocol and boolean enabled')
    pipeline = config.get('phase_config', {}).get('pipeline', [])
    if [p.get('name') for p in pipeline] != ['VIC_ONLY', 'ROUTING_ONLY', 'JOINT']:
        raise ValueError('Standalone full-mechanism profiles require VIC_ONLY, ROUTING_ONLY, JOINT in order')
    for phase in pipeline:
        for key in ('max_rounds', 'mc_rounds', 'mc_candidates'):
            if type(phase.get(key)) is not int:
                raise ValueError('Phase quantities must be JSON integers: ' + key)
        if phase['max_rounds'] < 1 or not 0 <= phase['mc_rounds'] <= phase['max_rounds']:
            raise ValueError('Invalid phase length / MC slots')
        if (phase['mc_rounds'] == 0) != (phase['mc_candidates'] == 0):
            raise ValueError('MC slots and candidates must both be zero, or both be positive')
        if phase['mc_rounds'] and phase['mc_candidates'] < 2:
            raise ValueError('An MC round needs at least two candidates')
        if method == 'SADR' and phase['mc_candidates'] > 5:
            raise ValueError('SADR code Worker supports at most five MC candidates')
        if phase.get('patience_rounds') is not None:
            raise ValueError('These matched fixed-budget profiles require patience_rounds=null')
    phases.load_phase_pipeline(config)
    rounds = sum(p['max_rounds'] for p in pipeline)
    if type(config.get('max_rounds')) is not int or config['max_rounds'] != rounds or rounds < 5:
        raise ValueError('max_rounds must equal the JSON pipeline sum, at least five')
    topology = contract.validate_topology(config)
    station_count = len(topology['station_ids'])
    if config.get('transport_protocol') != 'api':
        raise ValueError('This entry requires the native API transport')
    variant = config.get('comparison_variant')
    expected = {'E_refined': False}
    if variant not in expected or config.get('context_efficiency') != {
            'deduplicate_feedback_records': expected.get(variant), 'lossless_history': expected.get(variant)}:
        raise ValueError('Context delivery and context-efficiency settings disagree')
    if config.get('context_delivery_protocol') != {'protocol':'minimal_context_delivery_v1', 'arm':'E_refined'}:
        raise ValueError('Unsupported context delivery protocol')
    for key in ('runtime_audit', 'routing_cache'):
        if config.get(key, {}).get('enabled') is not True:
            raise ValueError('Complete mechanism profile requires enabled ' + key)
    if config.get('llm_failure_policy', {}).get('mode') != 'historical_fallback':
        raise ValueError('llm_failure_policy.mode must be historical_fallback')
    if config.get('model_binding', {}).get('mode') != 'real':
        raise ValueError('This launcher requires real model binding')
    normalized = protocol.normalize(config['initial_param_center'], config['param_ranges'])
    if normalized != config['initial_param_center']:
        raise ValueError('Initial center differs from its legal executable quantization')
    # runtime_context checks the same ranges and precision contract used at execution.
    protocol.runtime_context(config)
    exploration = config.get('initial_exploration', {})
    counts = [exploration.get('n_near'), exploration.get('n_random')]
    if (exploration.get('enabled') is not True or exploration.get('mode') not in ('fresh', 'reuse')
            or any(type(value) is not int or value < 0 for value in counts) or sum(counts) < 1):
        raise ValueError('Exploration requires nonnegative integer counts with positive total, and fresh or reuse mode')
    exploration_information = (exploration['n_near'] + exploration['n_random']) * station_count
    physical = exploration_information if exploration['mode'] == 'fresh' else 0
    budget = contract.expected_candidate_budget(config, rounds, exploration_requests=physical)
    budget['logical_llm_calls'] = {'leader': rounds, 'worker': budget['main_loop'] - 2 * station_count if method == 'MADR' else 0}
    budget['routing_fixed_flux_preparations'] = station_count
    budget['exploration_information_evaluations'] = exploration_information
    budget['topology'] = topology
    budget['mc_round_ids'] = [r['round_id'] for r in budget['rounds'] if r['per_station'] > 1]
    budget['coverage_note'] = ('Five rounds cover all phases and both MC stages, but not every stage-specific single-candidate branch.'
                               if rounds == 5 else 'Default first round and force-best last round are included in max_rounds.')
    return budget


def load_spec(config_path):
    source = Path(config_path).expanduser().resolve(strict=True)
    launch, settings_source = resolve_launch(source)
    for key in ('team_config', 'binding_json', 'work_root', 'run_name', 'api_profile'):
        if not isinstance(launch.get(key), str) or not launch[key]:
            raise ValueError('Missing launch field: ' + key)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', launch['run_name']):
        raise ValueError('run_name must contain only letters, digits, underscore, dot or hyphen')
    if launch['run_name'] in ('.', '..'):
        raise ValueError('Invalid run_name')
    team_path = resolve_config_path(launch['team_config'], source)
    cfg = copy.deepcopy(read_json(team_path))
    no_embedded_credentials(cfg)
    profile = launch['api_profile']
    if profile not in API_PROFILES:
        raise ValueError('Unknown API profile; choose ds_default, sonnet_cached, sonnet_default or gemini_default')
    configure_provider(cfg, profile)  # Provider fields only; JSON phases remain untouched.
    work = resolve_config_path(launch['work_root'], source)
    if any(c.isspace() for c in str(work)) or overlaps(work, PACKAGE_ROOT):
        raise ValueError('Use a separate work_root without whitespace, outside the code package')
    cfg.update(run_name=launch['run_name'], output_root=str(work/'runs'/launch['run_name']))
    exploration = launch.get('exploration', {})
    if not isinstance(exploration, dict) or set(exploration) - {'mode', 'source'}:
        raise ValueError('exploration accepts only mode and source')
    mode = exploration.get('mode')
    if mode not in ('fresh', 'reuse'):
        raise ValueError('Choose exploration.mode=fresh or reuse explicitly')
    cfg['initial_exploration']['mode'] = mode
    cfg['initial_exploration'].pop('source', None)
    if mode == 'reuse':
        if not exploration.get('source'):
            raise ValueError('Reuse requires a complete exploration bundle path')
        exploration_source = resolve_config_path(exploration['source'], source)
        bundle = read_json(exploration_source)
        if bundle.get('schema') != 'madr-exploration-bundle-v1' or bundle.get('complete') is not True:
            raise ValueError('Reuse requires a complete, authenticated exploration bundle')
    elif exploration.get('source'):
        raise ValueError('Fresh exploration must not name a reuse source')
    budget = validate_team(cfg)
    return {'source':source, 'launch':launch, 'team_path':team_path, 'config':cfg,
            'work':work, 'budget':budget,
            'network_policy':provider_network_policy(cfg['leader']['provider']),
            'settings_source':settings_source, 'framework_relative':framework_path(cfg['method'])}


def source_files(method='MADR'):
    paths = []
    for folder in (ROOT/'tools', ROOT/'scripts', ROOT/'prompts', ROOT/'schemas'):
        paths += [p for p in folder.rglob('*') if p.is_file() and not p.is_symlink()
                  and '__pycache__' not in p.parts and not p.name.startswith('.')
                  and p.suffix in ('.py', '.txt', '.md', '.json')]
    return {str(p.relative_to(ROOT)):sha256(p) for p in sorted(set(paths))}


def prepare(config_path):
    spec = load_spec(config_path)
    work, cfg = spec['work'], spec['config']
    if work.exists():
        raise ValueError('Existing work_root is never overwritten; use a fresh path')
    binding_path = resolve_config_path(spec['launch']['binding_json'], spec['source'])
    rows = binding_rows(binding_path, expected_stations={w['station_code'] for w in cfg['workers']})
    for row in rows.values():
        for key in ('soil_base', 'global_base', 'rout_template_dir'):
            parent = Path(row[key]) if key == 'rout_template_dir' else Path(row[key]).parent
            if work.is_relative_to(parent.resolve()):
                raise ValueError('work_root must not be inside shared physical inputs')
    plan = InputPlan(work)
    workers_by_code = {w['station_code']: w for w in cfg['workers']}
    bindings = {code:plan.station(code, row, workers_by_code[code].get('flux_prefix', 'fluxes_'))
                for code, row in sorted(rows.items())}
    for worker in cfg['workers']:
        worker.update(bindings[worker['station_code']])
    outlet = next(w for w in cfg['workers'] if w['subbasin_id'] == cfg['outlet_subbasin_id'])
    cfg.update({k:outlet[k] for k in ('vic_exe','rout_exe')})
    sources = source_files(cfg['method'])
    work.mkdir(parents=True, exist_ok=False)
    for folder in ('configs','refs','logs','runs'):
        (work/folder).mkdir()
    for relative, target in plan.links.items():
        (work/relative).symlink_to(target)
    for relative, content in plan.files.items():
        target = work/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    snapshot = work/'source_snapshot_used'
    for relative, digest in sources.items():
        target = snapshot/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/relative, target)
        if sha256(target) != digest:
            raise ValueError('Source changed while snapshotting: ' + relative)
        target.chmod(0o444)
    frozen_config = work/'configs/team.json'
    write_new(frozen_config, cfg)
    write_new(work/'requested_launch.json', spec['launch'])  # Contains paths only, never key contents.
    exploration = spec['launch']['exploration']
    exploration_files = {}
    if exploration['mode'] == 'reuse':
        shared = work/'shared_exploration'
        shared.mkdir()
        original = shared/'source_original.json'
        source = resolve_config_path(exploration['source'], spec['source'])
        source_digest = sha256(source)
        shutil.copyfile(source, original)
        if sha256(original) != source_digest:
            raise ValueError('Exploration source changed during copy')
        original.chmod(0o444)
        with (work/'logs/exploration_certification.log').open('x') as log:
            result = subprocess.run([sys.executable, '-B', str(snapshot/'tools/run_madr.py'),
                '--certify-exploration', str(work)], cwd=snapshot/spec['framework_relative'],
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONHASHSEED':'0'},
                stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError('Exploration identity certification failed; inspect logs/exploration_certification.log. No native/API evaluation was requested. Keep this work directory for audit and use a new one.')
        cfg['initial_exploration']['source'] = str(shared/'published/exploration_bundle.json')
        write_json(frozen_config, cfg)
        exploration_files = {str(p.relative_to(work)):sha256(p) for p in shared.rglob('*')
                             if p.is_file() and '__pycache__' not in p.parts}
    manifest = {'schema_version':1, 'release':RELEASE, 'work_root':str(work),
        'run_name':cfg['run_name'], 'method':cfg['method'], 'framework_relative':spec['framework_relative'],
        'settings_source':str(spec['settings_source']) if spec['settings_source'] else None,
        'settings_sha256':sha256(spec['settings_source']) if spec['settings_source'] else None,
        'resolved_launch':spec['launch'], 'requested_launch_sha256':sha256(spec['source']),
        'requested_team_sha256':sha256(spec['team_path']), 'config_relative':'configs/team.json',
        'config_sha256':sha256(frozen_config), 'snapshot_files':sources,
        'network_policy':spec['network_policy'], 'budget':spec['budget'],
        'binding_source_sha256':sha256(binding_path),
        'private_input_sha256':{r:sha256(work/r) for r in plan.files},
        'external_file_sha256':plan.externals, 'forcing_inventory':plan.forcing,
        'forcing_inventory_scope':'size and mtime at preparation; runtime independently hashes actual forcing contents',
        'input_aliases':plan.links, 'exploration_files':exploration_files,
        'exploration_mode':exploration['mode'], 'created_unix':time.time()}
    write_new(work/'standalone_manifest.json', manifest)
    return manifest


def certify_exploration(work):
    """Certify exact input/design/code identity without evaluating models or calling APIs."""
    from shared_exploration import _worker, expected_information, validate_reuse_receipt
    work = Path(work).resolve(strict=True)
    shared = work/'shared_exploration'
    published = shared/'published'
    published.mkdir(exist_ok=False)
    cfg = work/'configs/team.json'
    original = shared/'source_original.json'
    shutil.copyfile(original, published/'exploration_bundle.json')
    (published/'exploration_bundle.json').chmod(0o444)
    output = shared/'reuse_validation'
    _worker(cfg, output, source=published/'exploration_bundle.json')
    validate_reuse_receipt(read_json(output/'exploration_receipt.json'), expected_information(read_json(cfg)))


def verify_prepared(spec):
    work = spec['work']
    manifest = read_json(work/'standalone_manifest.json')
    if manifest.get('release') != RELEASE or manifest.get('work_root') != str(work):
        raise ValueError('Prepared identity changed or work_root moved')
    if (sha256(spec['source']) != manifest['requested_launch_sha256'] or
            sha256(spec['team_path']) != manifest['requested_team_sha256']):
        raise ValueError('Launch/team JSON changed after prepare; use a new work_root')
    if (manifest.get('settings_sha256') != (sha256(spec['settings_source']) if spec['settings_source'] else None) or
            manifest.get('resolved_launch') != spec['launch']):
        raise ValueError('Server settings or resolved launch changed after prepare; use a new work_root')
    if manifest.get('method') != spec['config']['method'] or manifest.get('framework_relative') != spec['framework_relative']:
        raise ValueError('Prepared method/framework identity changed')
    for relative, digest in manifest['snapshot_files'].items():
        source = work/'source_snapshot_used'/relative
        if source.is_symlink() or sha256(source) != digest:
            raise ValueError('Frozen source snapshot changed: ' + relative)
    for relative, digest in {manifest['config_relative']:manifest['config_sha256'],
                             **manifest['private_input_sha256'], **manifest['exploration_files']}.items():
        if (work/relative).is_symlink() or sha256(work/relative) != digest:
            raise ValueError('Frozen config/input/exploration changed: ' + relative)
    for relative, target in manifest['input_aliases'].items():
        if not (work/relative).is_symlink() or os.readlink(work/relative) != target:
            raise ValueError('Input alias changed: ' + relative)
    for path, digest in manifest['external_file_sha256'].items():
        if sha256(path) != digest:
            raise ValueError('Shared input or model executable changed: ' + path)
    for path, identity in manifest['forcing_inventory'].items():
        stat = Path(path).stat()
        if [stat.st_size, stat.st_mtime_ns] != identity:
            raise ValueError('Shared forcing size/mtime changed: ' + path)
    return manifest


def validate_completion(work, manifest, exit_code):
    output = work/'runs'/manifest['run_name']
    summary_path = output/'final_summary.json'
    summary = read_json(summary_path) if summary_path.is_file() else {}
    rounds = len(manifest['budget']['rounds'])
    errors = []
    if exit_code != 0:
        errors.append('framework_exit_nonzero')
    if summary.get('audit_complete') is not True:
        errors.append('core_audit_incomplete')
    if summary.get('rounds_executed') != rounds:
        errors.append('round_budget_incomplete')
    for row in manifest['budget']['rounds']:
        path = output/f"round_{row['round_id']:03d}/round_meta.json"
        meta = read_json(path) if path.is_file() else {}
        if meta.get('round_id') != row['round_id'] or meta.get('phase') != row['phase']:
            errors.append('phase_or_round_identity_incomplete')
            break
    if summary.get('final_confirmation_round') != rounds:
        errors.append('force_best_confirmation_missing')
    framework_modules(method=manifest.get('method', 'MADR'))
    recovery = importlib.import_module('scripts.recovery_accounting').summarize_recovery(output)
    return {'complete':not errors, 'errors':errors,
            'historical_fallback_calls':recovery['historical_fallback_calls'],
            'degraded':recovery['degraded'], 'llm_recovery_accounting':recovery,
            'core_audit_complete':summary.get('audit_complete'),
            'rounds_executed':summary.get('rounds_executed'),
            'best_mean_nse':summary.get('best_mean_nse'),
            'final_confirmation_mean_nse':summary.get('final_confirmation_mean_nse')}


def summarize(config_path):
    """Read actual usage with the saved JSON schedule; valid for any supported length."""
    from summarize_token_usage import audit_file
    spec = load_spec(config_path)
    manifest = verify_prepared(spec)
    work = spec['work']
    output = work/'runs'/manifest['run_name']
    ledger = output/'llm_calls.jsonl'
    if not ledger.is_file():
        raise ValueError('No API attempt ledger exists yet')
    usage = audit_file(ledger, schedule=manifest['budget']['rounds'], method=manifest['method'])
    reports = work/'reports'
    reports.mkdir(exist_ok=True)
    write_json(reports/'token_usage.json', usage)
    result = {'release':RELEASE, 'work_root':str(work),
              'schedule_source':'frozen_team_json',
              'token_report':str(reports/'token_usage.json'),
              'actual_usage':usage['totals'],
              'final_summary':str(output/'final_summary.json'),
              'native_audit':str(output/'native_audit'),
              'candidate_and_attempt_accounting':'Physical and LLM attempts remain separate; unknown usage is not assumed free.'}
    write_json(reports/'standalone_summary.json', result)
    return result


def run(config_path):
    spec = load_spec(config_path)
    work = spec['work']
    key_file = spec['launch'].get('key_file')
    if key_file:
        key_file = resolve_config_path(key_file, spec['source'])
    environment = credential_environment(key_file, forbidden_roots=(PACKAGE_ROOT,work.parent))
    env, network = child_environment(spec['network_policy'], environment)
    key_name = spec['config']['leader']['api_key_env']
    if not env.get(key_name):
        raise ValueError('Required API key environment variable is empty: ' + key_name)
    if not work.exists():
        prepare(config_path)
    lock = (work/'standalone.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest = verify_prepared(spec)
        receipt_path = work/'launcher_receipt.json'
        output = work/'runs'/manifest['run_name']
        if receipt_path.exists() or output.exists():
            raise ValueError('Existing run/receipt is never resumed or overwritten; choose a new work_root')
        from preflight_linux import dependencies, inspect_native
        if platform.system() != 'Linux':
            raise ValueError('Real runs require the target Linux server; validate/prepare are separate offline actions')
        runtime = dependencies()
        frozen = read_json(work/manifest['config_relative'])
        native = sorted({w[k] for w in frozen['workers'] for k in ('vic_exe','rout_exe')})
        preflight = {'passed':True,'api_or_model_calls':0,'runtime':runtime,
                     'native':[inspect_native(p) for p in native], 'network_policy':network}
        write_json(work/'standalone_preflight.json', preflight)
        snapshot = work/'source_snapshot_used'
        command = [sys.executable, '-B', '-m', 'scripts.run_langgraph', '--team-config',
                   str(work/manifest['config_relative']), '--log-level', 'INFO']
        receipt = {'release':RELEASE, 'started_unix':time.time(), 'status':'running',
            'source':'source_snapshot_used', 'network_policy':network,
            'manifest_sha256':sha256(work/'standalone_manifest.json'),
            'exploration_mode':manifest['exploration_mode'],
            'failure_policy':'Framework-owned historical retry/fallback; no external timeout abort or algorithm substitution.'}
        write_new(receipt_path, receipt)
        process = None
        try:
            with (work/'logs/run.log').open('x') as log:
                process = subprocess.Popen(command, cwd=snapshot/spec['framework_relative'], env=env,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                receipt['pid'] = process.pid
                write_json(receipt_path, receipt)
                exit_code = process.wait()
            completion = validate_completion(work, manifest, exit_code)
            receipt.update(status=('completed_degraded' if completion['degraded'] else 'completed') if completion['complete'] else 'failed_or_incomplete',
                           exit_code=exit_code, completion=completion, ended_unix=time.time())
            write_json(receipt_path, receipt)
            if (output/'llm_calls.jsonl').is_file():
                try:
                    usage = summarize(config_path)
                    receipt['token_report'] = usage['token_report']
                except (ValueError, OSError, KeyError, TypeError) as error:
                    receipt['postrun_token_audit_error_type'] = type(error).__name__
                    receipt['status'] = 'completed_with_postrun_audit_error' if completion['complete'] else receipt['status']
                write_json(receipt_path, receipt)
            return receipt
        except BaseException as error:
            if process is not None and process.poll() is None:
                stop_group(process)
            receipt.update(status='interrupted_or_launch_failed', error_type=type(error).__name__, ended_unix=time.time())
            write_json(receipt_path, receipt)
            raise
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='Standalone launch JSON')
    parser.add_argument('--action', choices=('validate','prepare','run','summarize'), default='validate')
    parser.add_argument('--certify-exploration', help=argparse.SUPPRESS)
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt('Run interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.certify_exploration:
            certify_exploration(args.certify_exploration)
            return 0
        if not args.config:
            parser.error('--config is required')
        if args.action == 'validate':
            spec = load_spec(args.config)
            result = {'release':RELEASE, 'action':'validate', 'physical_inputs_checked':False,
                      'api_or_native_calls':0, 'method':spec['config']['method'], 'framework_relative':spec['framework_relative'],
                      'variant':spec['config']['comparison_variant'],
                      'api_profile':spec['launch']['api_profile'], 'work_root':str(spec['work']),
                      'budget':spec['budget'], 'resolved_config':spec['config'],
                      'resolved_launch':spec['launch'], 'settings_source':str(spec['settings_source']),
                      'team_path':str(spec['team_path']), 'config_path':str(spec['source']),
                      'network_policy':spec['network_policy']}
        elif args.action == 'prepare':
            result = prepare(args.config)
            result = {'release':RELEASE, 'action':'prepare', 'work_root':result['work_root'],
                      'api_or_native_calls':0, 'budget':result['budget']}
        elif args.action == 'summarize':
            result = summarize(args.config)
        else:
            result = run(args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if args.action != 'run' or result.get('status') in ('completed','completed_degraded') else 2
    except (ValueError, OSError, KeyError, TypeError) as error:
        # Errors do not include credential values; config files never contain secrets.
        parser.exit(2, type(error).__name__ + ': ' + str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
