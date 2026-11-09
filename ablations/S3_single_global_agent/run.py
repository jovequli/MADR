#!/usr/bin/env python3
"""Single global-agent comparison using the shared native/API execution layers."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import math
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'MADR/tools'))
sys.path.insert(0, str(ROOT / 'MADR'))

from api_credentials import credential_environment
from linux_common import (expected_provider_profile, provider_network_policy,
                          read_json, sha256, overlaps, PHYSICAL_KEYS)
from prepare_linux import InputPlan, binding_rows
from transport_runtime import child_environment, stop_group
from scripts.execution_protocol import (KEYS, SOIL_KEYS, DECIMALS, EFFECTIVE_BOUNDS, normalize,
                                        require_effective_params, validate_periods)
from scripts.llm_client import LLMClient, LLMClientConfig, LLMCallExhausted
from scripts.llm_failure_policy import summarize_attempt_usage, validate_policy
from scripts.native_audit import atomic_json, json_safe
from scripts.process_trace import llm_trace_context, new_id, append_event, get_trace_errors
from scripts.run_contract import validate_topology
from scripts.routing_utils import UpstreamRoutingInfo, find_direc_in_template
from scripts.vic_executor import VICExecutor, VICConfig
from scripts.vic_runner import UpstreamSoilInfo

PROTOCOL = 'single_global_agent_full_vector_v1'
LAUNCH_FIELDS = {'schema_version', 'settings_file', 'physical_config', 'run_name',
                 'api_profile', 'global_evaluations', 'history_window', 'random_seed',
                 'binding_json', 'work_root', 'key_file', '_comment'}
PHYSICAL_FIELDS = ('workers', 'outlet_subbasin_id', 'simulation_period', 'warmup_period',
                   'evaluation_period', 'initial_param_center', 'param_ranges')
FEEDBACK_METRICS = frozenset((
    'NSE', 'RMSE', 'PBIAS', 'KGE', 'peak_ratio', 'lowflow_ratio',
    'peak_timing_bias_days', 'peak_magnitude_bias_pct', 'recession_k_ratio',
    'baseflow_index_bias_pct', 'evaluation_start', 'evaluation_end',
    'n_scored_days', 'diagnostic_status'))


def resolve(value, source):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else source.parent / path).resolve()


def no_secrets(data):
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in {'api_key', 'authorization', 'api_secret', 'access_token'} and value:
                raise ValueError('Use environment credentials or an external key_file')
            no_secrets(value)
    elif isinstance(data, list):
        for value in data:
            no_secrets(value)


def response_schema(stations):
    schema = read_json(HERE / 'schemas/response.json')
    fields = {key: {'type': 'number'} for key in KEYS}
    station_schema = {'type': 'object', 'additionalProperties': False,
                      'required': list(KEYS), 'properties': fields}
    schema['properties']['parameters'].update(
        additionalProperties=False, required=list(stations),
        properties={sid: copy.deepcopy(station_schema) for sid in stations})
    return schema


def validate_response(data, station_ids):
    if not isinstance(data, dict) or set(data) != {'reasoning', 'parameters'}:
        raise ValueError('Exactly reasoning:string and parameters:object are required')
    if not isinstance(data['reasoning'], str):
        raise ValueError('reasoning must be a string')
    vector = data['parameters']
    if not isinstance(vector, dict) or set(vector) != set(station_ids):
        raise ValueError('parameters must contain exactly the configured station IDs')
    for sid, params in vector.items():
        if not isinstance(params, dict) or set(params) != set(KEYS):
            raise ValueError('Every station requires all eight named parameters: ' + sid)
        for key, value in params.items():
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value)):
                raise ValueError('Parameter must be a finite JSON number: ' + sid + '.' + key)


def load_spec(path):
    source = Path(path).expanduser().resolve(strict=True)
    launch = read_json(source)
    no_secrets(launch)
    if launch.get('schema_version') != 1 or set(launch) - LAUNCH_FIELDS:
        raise ValueError('Unknown S3 launch field or schema_version')
    name = launch.get('run_name', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in ('.', '..'):
        raise ValueError('run_name must be a safe nonempty path component')
    settings_path = resolve(launch['settings_file'], source) if launch.get('settings_file') else None
    settings = read_json(settings_path) if settings_path else {}
    no_secrets(settings)
    if settings and (settings.get('schema_version') != 1 or set(settings) - {
            'schema_version', 'binding_json', 'work_root', 'key_file', '_comment'}):
        raise ValueError('Unknown server settings field or schema_version')
    resolved = copy.deepcopy(launch)
    for key in ('binding_json', 'work_root', 'key_file'):
        if launch.get(key):
            resolved[key] = str(resolve(launch[key], source))
        elif settings.get(key):
            resolved[key] = str(resolve(settings[key], settings_path))
            if key == 'work_root':
                resolved[key] = str(Path(resolved[key]) / name)
    for key in ('binding_json', 'work_root', 'physical_config', 'api_profile'):
        if not resolved.get(key):
            raise ValueError('Missing launch/settings field: ' + key)
    work = Path(resolved['work_root'])
    if any(c.isspace() for c in str(work)) or overlaps(work, ROOT):
        raise ValueError('Use a work_root outside the source package, without whitespace')
    source_cfg_path = resolve(launch['physical_config'], source)
    source_cfg = read_json(source_cfg_path)
    no_secrets(source_cfg)
    cfg = {key: copy.deepcopy(source_cfg[key]) for key in PHYSICAL_FIELDS}
    # Only geography and executable inputs survive projection from the baseline.
    worker_fields = ('agent_id', 'subbasin_id', 'station_code', 'neighbors_downstream',
                     'is_outlet', 'flux_prefix', *PHYSICAL_KEYS)
    cfg['workers'] = [{key: row[key] for key in worker_fields if key in row}
                      for row in cfg['workers']]
    topology = validate_topology(cfg)
    validate_periods(cfg)
    if {k:tuple(v) for k, v in cfg['param_ranges'].items()} != EFFECTIVE_BOUNDS:
        raise ValueError('Declared ranges must match the common executable parameter bounds')
    if normalize(cfg['initial_param_center'], cfg['param_ranges']) != cfg['initial_param_center']:
        raise ValueError('Initial center must already have legal executable precision and bounds')
    require_effective_params(cfg['initial_param_center'])
    for key in ('global_evaluations', 'history_window'):
        value = launch.get(key)
        if type(value) is not int or value < 1:
            raise ValueError(key + ' must be a positive integer')
    if type(launch.get('random_seed')) is not int:
        raise ValueError('random_seed must be an integer run identity')
    policy = validate_policy(source_cfg.get('llm_failure_policy', {}))
    if policy.get('mode') != 'historical_fallback':
        raise ValueError('S3 uses the shared historical_fallback API retry contract')
    profile = expected_provider_profile(launch['api_profile'])
    cfg.update(protocol=PROTOCOL, run_name=name, global_evaluations=launch['global_evaluations'],
               history_window=launch['history_window'], random_seed=launch['random_seed'],
               llm_failure_policy=policy, agent=profile, output_root=str(work / 'results'))
    budget = {'global_evaluations':cfg['global_evaluations'],
              'per_station_evaluations':cfg['global_evaluations'],
              'station_count':len(topology['station_ids']),
              'total_model_evaluations':cfg['global_evaluations'] * len(topology['station_ids']),
              'planned_global_agent_calls':cfg['global_evaluations'] - 1,
              'exploration_evaluations':0, 'center_rechecks':0,
              'fixed_flux_preparations':0,
              'initialization_included':True, 'extra_final_confirmation':False}
    return {'source':source, 'launch':resolved, 'settings_path':settings_path,
            'physical_config_path':source_cfg_path, 'work':work, 'config':cfg,
            'budget':budget, 'network_policy':provider_network_policy(profile['provider'])}


def source_files():
    files = {}
    for base in (ROOT / 'MADR/scripts', ROOT / 'MADR/prompts', ROOT / 'MADR/schemas', ROOT / 'MADR/tools', HERE):
        for path in sorted(base.rglob('*')):
            if (path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts
                    and not path.name.startswith('.') and path.suffix in ('.py', '.json', '.md', '.txt', '.sh')):
                files[str(path.relative_to(ROOT))] = sha256(path)
    return files


def prepare(path):
    spec = load_spec(path)
    work, cfg = spec['work'], spec['config']
    if work.exists():
        raise ValueError('Existing work directory is never overwritten; choose a new run_name')
    rows = binding_rows(spec['launch']['binding_json'],
                        expected_stations={w['station_code'] for w in cfg['workers']})
    plan = InputPlan(work)
    for worker in cfg['workers']:
        row = rows[worker['station_code']]
        for key in ('soil_base', 'global_base', 'rout_template_dir'):
            parent = Path(row[key]) if key == 'rout_template_dir' else Path(row[key]).parent
            if work.is_relative_to(parent.resolve()):
                raise ValueError('work_root must not be inside shared physical inputs')
        worker.update(plan.station(worker['station_code'], row, worker.get('flux_prefix', 'fluxes_')))
    sources = source_files()
    work.mkdir(parents=True, exist_ok=False)
    for folder in ('refs', 'logs', 'results'):
        (work / folder).mkdir()
    for relative, target in plan.links.items():
        (work / relative).symlink_to(target)
    for relative, data in plan.files.items():
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for relative, digest in sources.items():
        target = work / 'source_snapshot_used' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
        if sha256(target) != digest:
            raise ValueError('Source changed during preparation')
        target.chmod(0o444)
    atomic_json(work / 'config.json', cfg)
    manifest = {'protocol':PROTOCOL, 'work_root':str(work), 'budget':spec['budget'],
                'launch':spec['launch'], 'source_snapshot':sources,
                'launch_sha256':sha256(spec['source']),
                'physical_config_sha256':sha256(spec['physical_config_path']),
                'settings_sha256':sha256(spec['settings_path']) if spec['settings_path'] else None,
                'config_sha256':sha256(work / 'config.json'),
                'binding_sha256':sha256(spec['launch']['binding_json']),
                'private_input_sha256':{relative:sha256(work / relative) for relative in plan.files},
                'external_file_sha256':plan.externals, 'input_aliases':plan.links,
                'forcing_inventory':plan.forcing, 'network_policy':spec['network_policy'],
                'created_unix':time.time()}
    atomic_json(work / 'manifest.json', manifest)
    return manifest


def verify_prepared(spec):
    work = spec['work']
    manifest = read_json(work / 'manifest.json')
    if manifest.get('protocol') != PROTOCOL or manifest.get('work_root') != str(work):
        raise ValueError('Prepared work identity changed')
    expected = {'launch_sha256':sha256(spec['source']),
                'physical_config_sha256':sha256(spec['physical_config_path']),
                'settings_sha256':sha256(spec['settings_path']) if spec['settings_path'] else None,
                'binding_sha256':sha256(spec['launch']['binding_json'])}
    if manifest['launch'] != spec['launch'] or any(manifest[k] != v for k, v in expected.items()):
        raise ValueError('Launch/settings/input binding changed after preparation')
    for relative, digest in manifest['source_snapshot'].items():
        target = work / 'source_snapshot_used' / relative
        if target.is_symlink() or sha256(target) != digest:
            raise ValueError('Frozen source changed: ' + relative)
    for relative, digest in {'config.json':manifest['config_sha256'], **manifest['private_input_sha256']}.items():
        if (work / relative).is_symlink() or sha256(work / relative) != digest:
            raise ValueError('Frozen configuration/input changed: ' + relative)
    for relative, target in manifest['input_aliases'].items():
        if not (work / relative).is_symlink() or os.readlink(work / relative) != target:
            raise ValueError('Physical input alias changed')
    for target, digest in manifest['external_file_sha256'].items():
        if sha256(target) != digest:
            raise ValueError('Shared executable/input changed')
    for target, identity in manifest['forcing_inventory'].items():
        stat = Path(target).stat()
        if [stat.st_size, stat.st_mtime_ns] != identity:
            raise ValueError('Shared forcing changed')
    return manifest


def system_text(config):
    topology = validate_topology(config)
    facts = {key:config[key] for key in ('simulation_period', 'warmup_period', 'evaluation_period', 'param_ranges')}
    facts.update(parameter_decimal_places=DECIMALS, depth_constraint='soil_d2 < soil_d3 after quantization',
                 station_ids=topology['station_ids'], upstream_map=topology['upstream_map'],
                 total_global_evaluations=config['global_evaluations'],
                 response_schema=response_schema(topology['station_ids']))
    return '\n\n'.join([(HERE / 'prompts/task.md').read_text(),
                         (HERE / 'prompts/parameters.md').read_text(),
                         'Execution facts and output contract:\n' + json.dumps(facts, separators=(',', ':'))])


def prompt_facts(evaluation_id, history, best, config):
    def feedback(row):
        if row is None:
            return None
        result = copy.deepcopy(row)
        for station in result['stations'].values():
            station['metrics'] = {key:value for key, value in station['metrics'].items() if key in FEEDBACK_METRICS}
        return result
    return {'evaluation_id':evaluation_id, 'remaining_including_this':config['global_evaluations']-evaluation_id+1,
            'best_complete_vector':feedback(best),
            'recent_evaluations':[feedback(row) for row in history[-config['history_window']:]],
            'task':'Return one complete joint parameter vector for evaluation.'}


def upstream_inputs(sid, vector, config, topology):
    """Compose deepest-first masks so headwater ownership wins overlapping cells."""
    ancestors = set()
    def visit(current):
        for parent in topology['upstream_map'][current]:
            if parent not in ancestors:
                ancestors.add(parent)
                visit(parent)
    visit(sid)
    order = topology['topological_order']
    ancestors = sorted(ancestors, key=order.index, reverse=True)
    by_id = {w['subbasin_id']:w for w in config['workers']}
    soils, routing = [], []
    for parent in ancestors:
        worker, params = by_id[parent], vector[parent]
        direction = find_direc_in_template(worker['rout_template_dir'])
        if not direction:
            raise ValueError('Missing upstream routing mask: ' + parent)
        soils.append(UpstreamSoilInfo(worker['soil_base'], {k:params[k] for k in SOIL_KEYS}))
        routing.append(UpstreamRoutingInfo(direction, params['velocity'], params['diffusivity']))
    return soils, routing


def make_executor(config):
    executor = VICExecutor('real')
    executor.configure_runtime_audit(config['output_root'], enabled=True,
                                     retain_candidates=True, center_reference_cache=False)
    for worker in config['workers']:
        sid = worker['subbasin_id']
        if not find_direc_in_template(worker['rout_template_dir']):
            raise ValueError('Missing station routing mask: ' + sid)
        executor.register_basin(sid, VICConfig(
            basin_name=sid, base_output_dir=str(Path(config['output_root']) / 'native' / sid),
            **{k:worker[k] for k in PHYSICAL_KEYS},
            start_year=config['simulation_period']['start_year'], end_year=config['simulation_period']['end_year'],
            eval_start_year=config['evaluation_period']['start_year'], eval_end_year=config['evaluation_period']['end_year'],
            flux_prefix=worker.get('flux_prefix', 'fluxes_')))
    issues = executor.validate_all()
    if issues:
        raise ValueError('Missing physical inputs: ' + json.dumps(issues))
    return executor


def make_client(config):
    profile, policy = config['agent'], config['llm_failure_policy']
    client = LLMClient(LLMClientConfig(
        **{k:profile[k] for k in ('model', 'provider', 'api_key_env', 'base_url', 'max_tokens', 'extra_params')},
        timeout_sec=policy.get('timeout_sec', profile['timeout_sec']),
        max_retries=policy.get('max_attempts', profile['max_attempts']),
        retry_delay_sec=policy.get('retry_delay_sec', profile['retry_delay_sec']),
        failure_mode='historical_fallback'))
    client.response_validator = lambda value: validate_response(value, [w['subbasin_id'] for w in config['workers']])
    return client


def execute(config, executor, client):
    """Run the S3 comparison with the given executor and LLM client."""
    root = Path(config['output_root'])
    root.mkdir(parents=True, exist_ok=True)
    if (root / 'evaluations.jsonl').exists():
        raise ValueError('Evaluation ledger already exists; restarting must use a new run_name')
    topology = validate_topology(config)
    ids, best, history, degraded = topology['station_ids'], None, [], []
    initial = {sid:copy.deepcopy(config['initial_param_center']) for sid in ids}
    system = system_text(config)
    atomic_json(root / 'effective_protocol.json', {'protocol':PROTOCOL, 'config':config})
    (root / 'system_prompt.txt').write_text(system, encoding='utf-8')
    for number in range(1, config['global_evaluations']+1):
        directory = root / f'evaluation_{number:03d}'
        directory.mkdir(exist_ok=False)
        reason, source, raw = 'Configured default initialization.', 'initial_default', copy.deepcopy(initial)
        if number > 1:
            user = prompt_facts(number, history, best, config)
            atomic_json(directory / 'request.json', user)
            call_id = new_id()
            try:
                with llm_trace_context(root / 'llm_calls.jsonl', call_id, 'global_agent',
                                       meta={'role':'global_agent', 'evaluation_id':number, 'protocol':PROTOCOL}):
                    response = client.chat([{'role':'user', 'content':json.dumps(user, separators=(',', ':'))}],
                                           system=system, json_schema=response_schema(ids))
                    validate_response(response.parsed, ids)
                raw, reason, source = response.parsed['parameters'], response.parsed['reasoning'], 'global_agent'
                atomic_json(directory / 'response.json', response.parsed)
            except LLMCallExhausted as error:
                raw = copy.deepcopy(best['parameters'] if best else initial)
                reason, source = 'API retries exhausted; repeat a known complete vector without a diagnostic proposal.', 'neutral_failure_repeat'
                record = {'evaluation_id':number, 'call_id':call_id, 'source':source,
                          'failure_class':error.failure_class, 'usage_summary':error.usage_summary,
                          'next_scheduled_call':'retry_LLM_normally'}
                degraded.append(record)
                append_event(root / 'llm_recoveries.jsonl', record)
        vector = {sid:normalize(raw[sid], config['param_ranges']) for sid in ids}
        atomic_json(directory / 'proposal.json', {'source':source, 'reasoning':reason,
                    'submitted_parameters':raw, 'effective_parameters':vector,
                    'normalization_changed':raw != vector})
        results = {}
        for sid in topology['topological_order']:
            soil, routing = upstream_inputs(sid, vector, config, topology)
            ok, metrics = executor.run(sid, number*100+1, 1, vector[sid],
                                      phase_tag=f's3_evaluation_{number:03d}',
                                      log_path=str(directory / (sid + '.log')),
                                      upstream_soil_info=soil, upstream_routing_info=routing)
            value = metrics.get('NSE')
            valid = bool(ok and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))
            results[sid] = {'valid':valid, 'metrics':json_safe(metrics)}
        complete = all(row['valid'] for row in results.values())
        mean = sum(row['metrics']['NSE'] for row in results.values())/len(ids) if complete else None
        row = {'evaluation_id':number, 'parameters':vector, 'stations':results,
               'complete':complete, 'mean_nse':mean, 'proposal_source':source}
        if complete and (best is None or mean > best['mean_nse']):
            best = copy.deepcopy(row)
        history.append(row)
        atomic_json(directory / 'result.json', row)
        if not append_event(root / 'evaluations.jsonl', row):
            raise RuntimeError('Cannot retain evaluation ledger')
        atomic_json(root / 'checkpoint.json', {'completed_evaluations':number, 'best':best,
                                               'degraded_evaluations':degraded})
    trace_errors = get_trace_errors(root)
    usage = summarize_attempt_usage(root)
    calls = set()
    if (root / 'llm_calls.jsonl').is_file():
        for line in (root / 'llm_calls.jsonl').read_text().splitlines():
            event = json.loads(line)
            if event.get('event') == 'attempt_request':
                calls.add(event['call_id'])
    api_calls_complete = len(calls) == config['global_evaluations'] - 1
    audit_complete = not any(row.get('critical') for row in trace_errors) and api_calls_complete
    failed_evaluations = sum(not result['valid'] for row in history for result in row['stations'].values())
    summary = {'protocol':PROTOCOL, 'evaluations_completed':len(history),
               'per_station_evaluations':len(history), 'total_model_evaluation_requests':len(history)*len(ids),
               'complete_vectors':sum(row['complete'] for row in history),
               'best_complete_vector':best, 'best_mean_nse':best['mean_nse'] if best else None,
               'selection':'maximum mean evaluation-period NSE of one complete evaluated vector; earliest tie',
               'degraded_evaluations':degraded, 'degraded':bool(degraded),
               'api_usage':usage, 'recorded_global_agent_calls':len(calls),
               'failed_station_evaluations':failed_evaluations,
               'native_mode':getattr(executor, 'mode', 'unknown'),
               'native_accounting':executor.get_accounting(),
               'trace_errors':trace_errors, 'audit_complete':audit_complete,
               'extra_final_confirmation':False}
    atomic_json(root / 'final_summary.json', summary)
    return summary


def run(path):
    if platform.system() != 'Linux':
        raise ValueError('Real S3 runs require Linux; validate/prepare remain separate offline actions')
    spec = load_spec(path)
    work = spec['work']
    if not work.exists():
        prepare(path)
    lock = (work / 's3.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return run_prepared(spec)
    finally:
        lock.close()


def run_prepared(spec):
    work = spec['work']
    verify_prepared(spec)
    if (work / 'launcher_receipt.json').exists() or (work / 'results/evaluations.jsonl').exists():
        raise ValueError('Existing execution is never resumed or overwritten; choose a new run_name')
    environment = credential_environment(spec['launch'].get('key_file'), forbidden_roots=(ROOT, work))
    environment, network = child_environment(spec['network_policy'], environment)
    required_key = spec['config']['agent']['api_key_env']
    if not environment.get(required_key):
        raise ValueError('Missing provider credential; no model evaluation started')
    from preflight_linux import dependencies, inspect_native
    runtime = dependencies()
    frozen_config = read_json(work / 'config.json')
    executables = sorted({w[k] for w in frozen_config['workers'] for k in ('vic_exe', 'rout_exe')})
    preflight = {'passed':True, 'api_or_model_calls':0, 'runtime':runtime,
                 'native':[inspect_native(path) for path in executables], 'network_policy':network}
    atomic_json(work / 'preflight.json', preflight)
    atomic_json(work / 'network_policy.json', network)
    entry = work / 'source_snapshot_used' / HERE.relative_to(ROOT) / 'run.py'
    receipt = {'status':'running', 'started_unix':time.time(), 'source':'source_snapshot_used',
               'protocol':PROTOCOL, 'manifest_sha256':sha256(work / 'manifest.json')}
    atomic_json(work / 'launcher_receipt.json', receipt)
    child = None
    try:
        with (work / 'logs/console.log').open('x') as log:
            child = subprocess.Popen([sys.executable, '-B', str(entry), '--execute', str(work / 'config.json')],
                                     env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            receipt['pid'] = child.pid
            atomic_json(work / 'launcher_receipt.json', receipt)
            code = child.wait()
    except BaseException as error:
        if child is not None and child.poll() is None:
            stop_group(child)
        receipt.update(status='interrupted_or_launch_failed', error_type=type(error).__name__, ended_unix=time.time())
        atomic_json(work / 'launcher_receipt.json', receipt)
        raise
    summary_path = work / 'results/final_summary.json'
    summary = read_json(summary_path) if summary_path.is_file() else {}
    complete = (code == 0 and summary.get('evaluations_completed') == spec['config']['global_evaluations']
                and summary.get('audit_complete') is True and summary.get('best_complete_vector') is not None)
    completion = {'complete':complete, 'exit_code':code, 'degraded':summary.get('degraded'),
                  'best_mean_nse':summary.get('best_mean_nse'), 'output':str(work / 'results')}
    receipt.update(status=('completed_degraded' if summary.get('degraded') else 'completed') if complete else 'failed_or_incomplete',
                   completion=completion, ended_unix=time.time())
    atomic_json(work / 'launcher_receipt.json', receipt)
    atomic_json(work / 'completion.json', completion)
    if not complete:
        raise RuntimeError('S3 did not complete; inspect its saved console and attempt records')
    return completion


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt('S3 run interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('validate', 'prepare', 'run'))
    parser.add_argument('config', nargs='?', default=str(HERE / 'run.json'))
    parser.add_argument('--execute', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.execute:
        if platform.system() != 'Linux':
            raise ValueError('Native S3 child execution requires Linux')
        config = read_json(args.execute)
        # The API client must initialize before the default model evaluation.
        client = make_client(config)
        result = execute(config, make_executor(config), client)
        print(json.dumps({'best_mean_nse':result['best_mean_nse'], 'degraded':result['degraded']}))
    elif args.command == 'validate':
        spec = load_spec(args.config)
        print(json.dumps({'protocol':PROTOCOL, 'budget':spec['budget'], 'work_root':str(spec['work'])}, indent=2))
    elif args.command == 'prepare':
        result = prepare(args.config)
        print(json.dumps({'work_root':result['work_root'], 'budget':result['budget']}, indent=2))
    elif args.command == 'run':
        print(json.dumps(run(args.config), indent=2))
    else:
        parser.error('Choose validate, prepare or run')


if __name__ == '__main__':
    main()
