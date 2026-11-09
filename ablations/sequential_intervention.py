"""Strict station-by-station calibration over the unchanged MADR runtime.

Each station performs fresh exploration and its complete local schedule before
its terminal effective parameter vector is frozen. Only execution scope and
agent task scope change; the shared local search and acceptance code is reused.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

PROTOCOL = 'strict-sequential-station-v1'
CONTEXT = 'sequential_station_context'
_INSTALLED = None


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def _hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _ancestors(sid, upstream_map):
    found = set()
    pending = list(upstream_map[sid])
    while pending:
        parent = pending.pop()
        if parent == sid or parent not in upstream_map:
            raise ValueError('Invalid sequential upstream topology')
        if parent not in found:
            found.add(parent)
            pending.extend(upstream_map[parent])
    return found


def configure(config):
    """Validate this protocol without changing the supplied baseline schedule."""
    from scripts.run_contract import validate_topology
    from .search_interventions import evaluation_schedule
    result = copy.deepcopy(config)
    if CONTEXT in result:
        raise ValueError('Configure an outer sequential run, not a station child')
    validate_topology(result)
    if result.get('method') != 'MADR':
        raise ValueError('This sequential arm is an MADR architecture comparison')
    explore = result.get('initial_exploration', {})
    if not explore.get('enabled') or explore.get('mode', 'fresh') != 'fresh':
        raise ValueError('Sequential exploration must run fresh after upstream stations freeze')
    if any(explore.get(key) for key in ('source', 'seed_from_run', 'reuse_path', 'reuse_dir')):
        raise ValueError('Sequential exploration cannot consume a previous exploration bundle')
    for key in ('n_near', 'n_random'):
        if type(explore.get(key)) is not int or explore[key] < 0:
            raise ValueError('Fresh exploration requires nonnegative integer sample counts')
    if explore['n_near'] + explore['n_random'] < 1:
        raise ValueError('Sequential exploration requires actual evaluations')
    phases = result['phase_config']['pipeline']
    if [row['name'] for row in phases] != ['VIC_ONLY', 'ROUTING_ONLY', 'JOINT']:
        raise ValueError('Sequential arm preserves the complete three-phase local schedule')
    if any(row.get('patience_rounds') is not None for row in phases):
        raise ValueError('Sequential matched comparisons require the fixed round budget')
    rows = evaluation_schedule(result)
    if result.get('max_rounds') != len(rows):
        raise ValueError('Local max_rounds must match the complete phase pipeline')
    if result.get('full_upstream_map') or result.get('fixed_upstream_routing'):
        raise ValueError('Declare all upstream stations in workers; external pre-fixed basins are unsupported')
    return result


def budget(config):
    from scripts.run_contract import validate_topology
    from .search_interventions import evaluation_schedule
    config = configure(config)
    topology = validate_topology(config)
    local = evaluation_schedule(config)
    rows = []
    for sid in topology['topological_order']:
        for row in local:
            rows.append({**row, 'round_id': len(rows) + 1, 'local_round_id': row['round_id'],
                         'subbasin_id': sid, 'candidate_evaluations': row['per_station']})
    n = len(config['workers'])
    samples = config['initial_exploration']['n_near'] + config['initial_exploration']['n_random']
    main = sum(row['candidate_evaluations'] for row in rows)
    return {'main_loop': main, 'exploration': n * samples, 'center_reference_slots': 0,
            'center_rechecks': 0, 'center_rounds': [], 'total': main + n * samples,
            'total_evaluations_upper_bound': main + n * samples,
            'rounds': rows, 'rounds_per_station': len(local),
            'station_order': topology['topological_order'], 'leader_calls': len(rows),
            'worker_proposal_calls': main - 2 * n, 'fixed_flux_preparations': n,
            'base_evaluations_per_station': main // n + samples,
            'count_semantics': 'Sequential local candidate slots plus fresh exploration. Fixed-flux preparation and retries are separate. Immutable upstream backgrounds need no additional center reevaluations.',
            'round_identity': '(subbasin_id, local_round_id)'}


def _background_identity(context):
    return {'protocol': PROTOCOL, 'active_station': context['active_station'],
            'original_upstream_map': context['original_upstream_map'],
            'frozen_ancestors': {sid: {key: row.get(key) for key in (
                'effective_parameters', 'selection_sha256', 'execution_record_sha256', 'result_sha256')}
                for sid, row in sorted(context['frozen_ancestors'].items())}}


def make_station_config(config, sid, frozen, output_root):
    """Project one active execution scope, retaining its true external topology."""
    from scripts.run_contract import validate_topology
    topology = validate_topology(config)
    if sid not in topology['upstream_map']:
        raise ValueError('Unknown active station')
    ancestors = _ancestors(sid, topology['upstream_map'])
    if not ancestors.issubset(frozen):
        raise ValueError('Cannot start a station before every upstream ancestor has frozen')
    result = copy.deepcopy(config)
    original = copy.deepcopy(config['workers'])
    active = next(copy.deepcopy(w) for w in original if w['subbasin_id'] == sid)
    # These are scheduling identities only. Physical builders and diagnostics use
    # original_upstream_map; nested stations are not diagnosed as headwaters.
    active.update(neighbors_downstream=[], is_outlet=True)
    result.update(workers=[active], outlet_subbasin_id=sid, max_parallel_workers=1,
                  objective_weights={'mean_nse': 1.0, 'consistency': 0.0},
                  output_root=str(Path(output_root).resolve()),
                  run_name=str(config['run_name']) + '__' + sid)
    context = {'protocol': PROTOCOL, 'active_station': sid,
               'original_workers': original, 'original_upstream_map': topology['upstream_map'],
               'original_outlet': config['outlet_subbasin_id'],
               'frozen_ancestors': {up: {key: copy.deepcopy(value) for key, value in frozen[up].items()
                                       if key not in ('summary', 'rounds_executed', 'terminal_confirmation')}
                                    for up in sorted(ancestors)}}
    context['frozen_context_sha256'] = _fingerprint(_background_identity(context))
    result[CONTEXT] = context
    validate_station_context(result)
    return result


def validate_station_context(config):
    from scripts.execution_protocol import require_effective_params
    from scripts.run_contract import validate_topology
    context = config.get(CONTEXT)
    if not isinstance(context, dict) or context.get('protocol') != PROTOCOL:
        raise ValueError('Sequential station context is missing')
    sid = context['active_station']
    if [w.get('subbasin_id') for w in config.get('workers', [])] != [sid]:
        raise ValueError('Sequential child must have exactly its one active station')
    original_cfg = {**config, 'workers': context['original_workers'],
                    'outlet_subbasin_id': context['original_outlet']}
    topology = validate_topology(original_cfg)
    if context['original_upstream_map'] != topology['upstream_map']:
        raise ValueError('Saved original topology disagrees with original workers')
    expected = _ancestors(sid, topology['upstream_map'])
    if set(context['frozen_ancestors']) != expected:
        raise ValueError('Frozen context must contain every recursive ancestor exactly once')
    if context.get('frozen_context_sha256') != _fingerprint(_background_identity(context)):
        raise ValueError('Frozen background fingerprint differs from its parameter/source records')
    real = config.get('model_binding', {}).get('mode', 'real') == 'real'
    originals = {w['subbasin_id']: w for w in context['original_workers']}
    from scripts.routing_utils import find_direc_in_template
    for up, row in context['frozen_ancestors'].items():
        require_effective_params(row['effective_parameters'])
        record = row.get('confirmed_record', {})
        if record.get('ok') is not True:
            raise ValueError('Frozen ancestor lacks a successful terminal confirmation: ' + up)
        if (record.get('params') != row['effective_parameters']
                or record.get('metrics') != row.get('metrics')):
            raise ValueError('Frozen ancestor diagnostics differ from its confirmed vector: ' + up)
        for key, hashkey in (('selection_file', 'selection_sha256'),
                             ('result_file', 'result_sha256'),
                             ('execution_record_file', 'execution_record_sha256')):
            source = row.get(key)
            if source is not None and (not Path(source).is_file() or _hash(source) != row.get(hashkey)):
                raise ValueError('Frozen evidence is missing or changed: ' + up + ':' + key)
        if row.get('result_file'):
            raw = _read(row['result_file'])
            expected_record = copy.deepcopy(raw['result'])
            for key in ('upstream_context_id', 'execution_identity', 'result_scope', 'routing_result_relative'):
                if key in raw:
                    expected_record[key] = copy.deepcopy(raw[key])
            if record != expected_record:
                raise ValueError('Frozen ancestor diagnostic record differs from its source: ' + up)
        if real:
            if not row.get('execution_record_file') or not isinstance(row.get('native_record'), dict):
                raise ValueError('Real frozen ancestor requires native execution evidence')
            if not Path(originals[up].get('soil_base', '')).is_file():
                raise ValueError('Frozen upstream soil source missing: ' + up)
            if not find_direc_in_template(originals[up].get('rout_template_dir', '')):
                raise ValueError('Frozen upstream routing direction missing: ' + up)
    return copy.deepcopy(context)


def _frozen_diagnostic_records(context):
    """Direct-parent observations, measured under their permanently frozen ancestors."""
    records = {}
    for up in context['original_upstream_map'][context['active_station']]:
        row = context['frozen_ancestors'][up]
        record = row['confirmed_record']
        if record.get('params') != row['effective_parameters']:
            raise ValueError('Frozen diagnostic parameters differ: ' + up)
        records[up] = copy.deepcopy(record)
    return records


def _frozen_diagnostic_context(context, records):
    from scripts.execution_consistency import finite_success
    upstream = context['original_upstream_map'][context['active_station']]
    known = bool(upstream) and all(finite_success(records.get(up)) for up in upstream)
    quality = ('poor' if any(records[up]['metrics']['NSE'] < .5 for up in upstream)
               else 'good') if known else 'unknown'
    return {'is_headwater': not bool(upstream), 'upstream_quality': quality}


def install_station(config):
    """Install exact local adapters before constructing the shared graph."""
    global _INSTALLED
    context = validate_station_context(config)
    sid = context['active_station']
    frozen_diagnostics = _frozen_diagnostic_records(context)
    diagnostic_context = _frozen_diagnostic_context(context, frozen_diagnostics)
    if _INSTALLED is not None:
        raise RuntimeError('Each station must run in a separate process')
    from scripts import graph_nodes as graph, input_fingerprint, representative_result
    from scripts import current_evidence, exploration_reuse, initial_explorer
    from scripts.worker_agent import WorkerAgent
    changes = []

    def patch(owner, name, value):
        changes.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def physical_state(state):
        centers = {up: copy.deepcopy(row['effective_parameters'])
                   for up, row in context['frozen_ancestors'].items()}
        centers[sid] = copy.deepcopy(state['centers'][sid])
        projected = dict(state, centers=centers,
            upstream_map=context['original_upstream_map'], full_upstream_map=context['original_upstream_map'],
            topo_levels=graph._compute_topo_levels(context['original_upstream_map']))
        return projected

    for name in ('_build_upstream_soil_info', '_build_upstream_routing_info'):
        original = getattr(graph, name)
        def builder(subbasin_id, state, worker_cfgs, _original=original):
            if subbasin_id != sid:
                raise ValueError('Inactive station requested a sequential physical execution')
            result = _original(subbasin_id, physical_state(state), context['original_workers'])
            if len(result) != len(context['frozen_ancestors']):
                raise ValueError('Sequential physical upstream overlay omitted an ancestor')
            return result
        patch(graph, name, builder)

    original_report = WorkerAgent.report_diagnostics
    def report(agent, metrics, prev_metrics=None, context=None):
        if agent.subbasin_id != sid:
            raise ValueError('Inactive station received sequential diagnostics')
        local_context = dict(context or {}, **diagnostic_context)
        return original_report(agent, metrics, prev_metrics, local_context)
    patch(WorkerAgent, 'report_diagnostics', report)

    original_prepare = current_evidence.prepare
    def prepare(agent, state, reference, before):
        if agent.subbasin_id != sid:
            raise ValueError('Inactive station received sequential current-reference diagnostics')
        records = dict(state.get('accepted_center_records', {}))
        records.update(copy.deepcopy(frozen_diagnostics))
        local = dict(state, upstream_map=context['original_upstream_map'],
                     worker_cfgs=context['original_workers'], accepted_center_records=records)
        # Only the shared diagnostic quality lookup sees these immutable records;
        # graph state, active workers and the local-only agent panel stay unchanged.
        return original_prepare(agent, local, reference, before)
    patch(current_evidence, 'prepare', prepare)

    sampling_order = [station for level in graph._compute_topo_levels(context['original_upstream_map'])
                      for station in level]
    sampling_position = sampling_order.index(sid)
    original_explore = exploration_reuse.run_initial_exploration
    def fresh_exploration(orchestrator, n_near=10, n_random=40, vic_only_keys=None):
        # run_exploration_protocol has seeded its isolated RNG stream.
        # Consume only the ordinary sampler draws for preceding stations, never
        # their model evaluations or metrics. Candidate coordinates thus match
        # the same-seed baseline while this station's scores remain freshly run.
        from scripts.execution_protocol import SOIL_KEYS
        keys = list(vic_only_keys) if vic_only_keys is not None else list(SOIL_KEYS)
        center = copy.deepcopy(orchestrator.centers[sid])
        for _ in range(sampling_position):
            initial_explorer._generate_near_samples(center, orchestrator.param_ranges, keys, n_near)
            initial_explorer._generate_random_samples(orchestrator.param_ranges, keys, center, n_random)
        return original_explore(orchestrator, n_near=n_near, n_random=n_random, vic_only_keys=keys)
    patch(exploration_reuse, 'run_initial_exploration', fresh_exploration)

    original_identity = input_fingerprint.build_input_identity
    def identity(orchestrator, cfg, design):
        result, provenance = original_identity(orchestrator, cfg, design)
        frozen = _background_identity(context)
        frozen['adapter_sha256'] = _hash(__file__)
        frozen['sampling_position'] = sampling_position
        frozen['baseline_sampling_order'] = sampling_order
        frozen['sampling_semantics'] = 'Same-seed ordinary exploration candidate coordinates; scores are fresh under frozen upstream inputs.'
        originals = {w['subbasin_id']: w for w in context['original_workers']}
        if result['execution_mode'] == 'real':
            frozen['physical_templates'] = {up: {
                'soil': input_fingerprint.file_identity(originals[up]['soil_base']),
                'routing': input_fingerprint._routing_identity(originals[up]['rout_template_dir'], {})}
                for up in sorted(context['frozen_ancestors'])}
        result['sequential_frozen_background'] = frozen
        provenance['sequential_frozen_evidence'] = {up: {
            key: row.get(key) for key in ('source_run_root', 'selection_file', 'execution_record_file', 'result_file')}
            for up, row in context['frozen_ancestors'].items()}
        return result, provenance
    patch(input_fingerprint, 'build_input_identity', identity)
    # Exploration imports this function directly; update that bound alias too.
    patch(exploration_reuse, 'build_input_identity', identity)

    original_audit = representative_result.audit_upstream_vector
    def audit(cfg, station_results, records, mode='real'):
        if CONTEXT not in cfg:
            return original_audit(cfg, station_results, records, mode)
        validate_station_context(cfg)
        expected = {sid} | set(context['frozen_ancestors'])
        original_workers = copy.deepcopy(context['original_workers'])
        selected_workers = [w for w in original_workers if w['subbasin_id'] in expected]
        for w in selected_workers:
            w['neighbors_downstream'] = [d for d in w.get('neighbors_downstream', []) if d in expected]
        all_results, all_records = copy.deepcopy(station_results), copy.deepcopy(records)
        for up, row in context['frozen_ancestors'].items():
            all_results[up] = {'effective_parameters': row['effective_parameters']}
            all_records[up] = row.get('native_record') or {}
        return original_audit({**cfg, 'workers': selected_workers}, all_results, all_records, mode)
    patch(representative_result, 'audit_upstream_vector', audit)
    from .sequential_prompts import install as install_prompts
    restore_prompts = install_prompts(config)
    _INSTALLED = sid

    def restore():
        global _INSTALLED
        if callable(restore_prompts):
            restore_prompts()
        for owner, name, old in reversed(changes):
            setattr(owner, name, old)
        _INSTALLED = None
    return restore


def _freeze(sid, root, config):
    root = Path(root)
    summary = _read(root / 'final_summary.json')
    selection_file = root / 'final_confirmation_result/selection.json'
    selection = _read(selection_file)
    if (summary.get('audit_complete') is not True or selection.get('audit_complete') is not True
            or selection.get('selection_complete') is not True
            or set(selection.get('station_results', {})) != {sid}
            or summary.get('rounds_executed') != config['max_rounds']
            or selection.get('selected_round_id') != config['max_rounds']
            or selection.get('terminal_confirmation', {}).get('force_best_all_stations') is not True):
        raise ValueError('Cannot freeze an incomplete or unaudited station: ' + sid)
    chosen = selection['station_results'][sid]
    result_file = root / chosen['result_file']
    raw = _read(result_file)
    record = copy.deepcopy(raw['result'])
    for key in ('upstream_context_id', 'execution_identity', 'result_scope', 'routing_result_relative'):
        if key in raw:
            record[key] = copy.deepcopy(raw[key])
    if (record.get('ok') is not True or record.get('params') != chosen['effective_parameters']
            or record.get('metrics') != chosen['metrics'] or not math.isfinite(chosen['NSE'])):
        raise ValueError('Terminal station selection differs from its actual result')
    native_file = selection.get('execution_evidence', {}).get(sid, {}).get('execution_record')
    native_file = root / native_file if native_file else None
    return {'effective_parameters': chosen['effective_parameters'], 'metrics': chosen['metrics'],
            'confirmed_record': record, 'source_run_root': str(root.resolve()),
            'selection_file': str(selection_file.resolve()), 'selection_sha256': _hash(selection_file),
            'result_file': str(result_file.resolve()), 'result_sha256': _hash(result_file),
            'execution_record_file': str(native_file.resolve()) if native_file else None,
            'execution_record_sha256': _hash(native_file) if native_file else None,
            'native_record': _read(native_file) if native_file else None,
            'rounds_executed': summary['rounds_executed'], 'summary': summary,
            'terminal_confirmation': selection['terminal_confirmation']}


def _merge_calls(output_root, order):
    output_root = Path(output_root)
    destination = output_root / 'llm_calls.jsonl'
    temporary = destination.with_name('.llm_calls.jsonl.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        for sid in order:
            path = output_root / 'stations' / sid / 'llm_calls.jsonl'
            if not path.is_file():
                continue
            for line in path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    row['station_run'] = sid
                    call_context = row.get('context') or {}
                    row['local_round_id'] = call_context.get('round_id', call_context.get('observed_round_id', row.get('round_id')))
                    row['station_id'] = sid
                    stream.write(json.dumps(row, allow_nan=False) + '\n')
    temporary.replace(destination)


def execute(config, output_root, *, run_station=None):
    """Run complete local tasks topologically, freezing only audited confirmations.

    run_station can be supplied by the caller. It receives (station_config,
    station_output_root) and must write the unchanged core's audited outputs.
    """
    from scripts.run_contract import validate_topology
    from scripts.representative_result import audit_upstream_vector
    config = configure(config)
    output_root = Path(output_root).resolve()
    if (output_root / 'sequential_manifest.json').exists():
        raise FileExistsError('Sequential output already has a run manifest; choose a fresh run root')
    output_root.mkdir(parents=True, exist_ok=True)
    topology = validate_topology(config)
    order = topology['topological_order']
    planned = budget(config)
    manifest = {'protocol': PROTOCOL, 'station_order': order, 'planned_budget': planned,
                'status': 'running', 'completed_stations': [], 'active_station': None,
                'local_score': 'NSE at the active station; legacy local J fields equal NSE',
                'seed_policy': 'Preserve configured seeds and each station position in the ordinary exploration sampling stream; re-evaluate the paired coordinates under frozen upstream inputs, with no old score reuse.',
                'final_selection': 'Each station confirms its maximum-NSE legal local trajectory, earliest exact tie, then freezes permanently.'}
    frozen = {}
    configs = {}
    _write(output_root / 'sequential_manifest.json', manifest)
    try:
        for sid in order:
            root = output_root / 'stations' / sid
            child = make_station_config(config, sid, frozen, root)
            configs[sid] = child
            cfg_path = output_root / 'station_configs' / (sid + '.json')
            _write(cfg_path, child)
            manifest.update(active_station=sid)
            _write(output_root / 'sequential_manifest.json', manifest)
            try:
                if run_station is None:
                    entry = Path(__file__).with_name('execute.py')
                    completed = subprocess.run([sys.executable, '-B', str(entry), '--team-config', str(cfg_path)], check=False)
                    if completed.returncode:
                        raise RuntimeError('Sequential station process failed: ' + sid)
                else:
                    run_station(child, root)
            finally:
                _merge_calls(output_root, order)
            validate_station_context(child)
            frozen[sid] = _freeze(sid, root, child)
            manifest['completed_stations'].append(sid)
            _write(output_root / 'frozen_stations.json', frozen)
            _write(output_root / 'sequential_manifest.json', manifest)
        mode = config.get('model_binding', {}).get('mode', 'real')
        final_stations = {sid: {'subbasin_id': sid, 'NSE': row['metrics']['NSE'],
            'metrics': row['metrics'], 'effective_parameters': row['effective_parameters'],
            'source_selection': str(Path(row['selection_file']).relative_to(output_root)),
            'source_selection_sha256': row['selection_sha256']} for sid, row in frozen.items()}
        audit = audit_upstream_vector(config, final_stations,
            {sid: row.get('native_record') or {} for sid, row in frozen.items()}, mode)
        if mode == 'real' and audit.get('consistent') is not True:
            raise ValueError('Final frozen parameter vector failed the original upstream audit')
        mean = math.fsum(row['NSE'] for row in final_stations.values()) / len(final_stations)
        selection = {'protocol': PROTOCOL, 'audit_complete': True, 'selection_complete': True,
            'selection_rule': manifest['final_selection'], 'station_results': final_stations,
            'mean_nse': mean, 'mode': mode, 'upstream_vector_audit': audit,
            'result_kind': 'sequential_frozen_station_confirmations',
            'reproduction_note': 'Every station was physically confirmed after all its ancestors froze. No additional model calls were made by this aggregate export. Source confirmations retain all native artifacts.'}
        for name in ('representative_result', 'final_confirmation_result'):
            _write(output_root / name / 'selection.json', selection)
            _write(output_root / name / 'effective_parameters.json', {sid: row['effective_parameters'] for sid, row in frozen.items()})
        summary = {'protocol': PROTOCOL, 'audit_complete': True, 'rounds_executed': sum(row['rounds_executed'] for row in frozen.values()),
            'station_order': order, 'best_mean_nse': mean, 'final_confirmation_mean_nse': mean,
            'final_confirmation': {'per_station': {sid: row['terminal_confirmation'] for sid, row in frozen.items()}},
            'planned_budget': planned, 'native_audit': audit, 'station_summaries': {sid: row['summary'] for sid, row in frozen.items()},
            'representative_selection': manifest['final_selection'],
            'round_count_semantics': 'Sum of separate station-local interaction rounds; not simultaneous global rounds.'}
        manifest.update(status='completed', active_station=None)
    except (Exception, KeyboardInterrupt) as error:
        _merge_calls(output_root, order)
        manifest.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', failure_type=type(error).__name__, failure_message=str(error))
        summary = {'protocol': PROTOCOL, 'audit_complete': False,
                   'rounds_executed': sum(row['rounds_executed'] for row in frozen.values()),
                   'completed_stations': list(frozen), 'failed_station': manifest.get('active_station'),
                   'error': str(error), 'planned_budget': planned}
        _write(output_root / 'final_summary.json', summary)
        _write(output_root / 'sequential_manifest.json', manifest)
        raise
    _write(output_root / 'final_summary.json', summary)
    _write(output_root / 'sequential_manifest.json', manifest)
    return summary
