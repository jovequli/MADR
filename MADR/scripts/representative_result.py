"""Offline representative-round selection/export; never changes online optimization.

The representative is the complete main round with greatest unrounded arithmetic
mean station NSE. Exact ties select the earliest round. Exploration and unselected
MC proposals never enter this comparison. Standard library only; no model calls.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

RESULT_EXPORT_PROTOCOL = 'tra_dual_knowledge_joint_result_v1'
SOIL_PARAMETERS = ('binfilt', 'Ds', 'Dsmax', 'Ws', 'soil_d2', 'soil_d3')
ROUTING_PARAMETERS = ('velocity', 'diffusivity')

ROUND_PATTERN = re.compile(r'^round_(\d+)$')


class RepresentativeError(RuntimeError):
    """No export was committed; selection or archived execution evidence failed."""
    def __init__(self, message, selection=None):
        super().__init__(message)
        self.selection = selection
        self.status = 'audit_incomplete'


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def _sha(path, uncompress=False):
    digest = hashlib.sha256()
    opener = gzip.open if uncompress else open
    with opener(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _safe_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise RepresentativeError(f'Unsafe archived relative path: {relative}')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise RepresentativeError(f'Archived path escapes run directory: {relative}')
    return path


def _identity(run):
    config_path = run / 'provenance/config.json'
    config = _read(config_path) if config_path.is_file() else {}
    run_manifest = _read(run / 'run_manifest.json') if (run / 'run_manifest.json').is_file() else {}
    final = _read(run / 'final_summary.json') if (run / 'final_summary.json').is_file() else {}
    mode = config.get('model_binding', {}).get('mode') or run_manifest.get('mode') or final.get('mode') or 'real'
    if mode not in ('real', 'simulate'):
        raise RepresentativeError(f'Unknown execution mode: {mode}')
    test_only = bool(config.get('test_only') or final.get('test_only') or run_manifest.get('dry_run') or mode != 'real')
    return config, {'mode': mode, 'test_only': test_only,
        'source_run_audit_complete': final.get('audit_complete'),
        'online_best_global_j_total': final.get('online_best_global_j_total') or (
            {'round_id': final.get('best_round'), 'score': final.get('best_global_score')}
            if 'best_global_score' in final else None)}


def select_representative_round(run_root, expected_rounds=None, expected_stations=None):
    """Return an explicit complete/incomplete selection using selected round results.

    Missing/invalid rounds are disclosed; their omission is never described as a
    complete experiment. expected_rounds bounds the eligible round IDs to 1..N.
    A short run may pass its own N. No files are written by this function.
    """
    run = Path(run_root).expanduser().resolve(strict=True)
    config, identity = _identity(run)
    if expected_rounds is not None and (type(expected_rounds) is not int or expected_rounds < 1):
        raise ValueError('expected_rounds must be a positive integer or None')
    stations = list(expected_stations) if expected_stations is not None else [w['subbasin_id'] for w in config.get('workers', [])]
    if not stations:
        raise ValueError('Representative selection requires configured or explicit station identities')
    if len(stations) != len(set(stations)) or any(not isinstance(s, str) or not s for s in stations):
        raise ValueError('Expected stations must be unique nonempty identifiers')
    stations = sorted(stations)
    directories = {}
    for path in run.iterdir():
        match = ROUND_PATTERN.fullmatch(path.name)
        if path.is_dir() and match:
            rid = int(match.group(1))
            if rid in directories:
                raise RepresentativeError(f'Duplicate main round ID: {rid}')
            directories[rid] = path
    stop = expected_rounds if expected_rounds is not None else max(directories, default=0)
    missing = [n for n in range(1, stop + 1) if n not in directories]
    unexpected = [n for n in sorted(directories) if n < 1 or n > stop]
    invalid, valid = [], []
    for rid in sorted(n for n in directories if 1 <= n <= stop):
        path = directories[rid]
        try:
            meta = _read(path / 'round_meta.json')
            if meta.get('round_id') != rid:
                raise ValueError('round_meta ID mismatch')
            station_results = {}
            result_files = sorted(p for p in (path / 'results').glob('*.json') if not p.name.startswith('._'))
            for source in result_files:
                record = _read(source)
                sid = record.get('subbasin_id')
                if sid not in stations or sid in station_results or record.get('round_id') != rid:
                    raise ValueError('duplicate/unexpected station or result round ID mismatch')
                selected = record.get('result', {})
                score = selected.get('metrics', {}).get('NSE')
                if selected.get('ok') is not True or not _finite(score):
                    raise ValueError(f'{sid}: selected result failed or NSE is not finite')
                params = selected.get('params')
                if not isinstance(params, dict) or not params or any(not _finite(v) for v in params.values()):
                    raise ValueError(f'{sid}: effective parameter vector missing/nonfinite')
                station_results[sid] = {'subbasin_id': sid, 'NSE': score, 'metrics': selected['metrics'],
                    'effective_parameters': params, 'winning_candidate_id': record.get('winning_candidate_id'),
                    'has_mc_selection': 'mc_detail' in record,
                    'result_scope': record.get('result_scope'),
                    'execution_identity': record.get('execution_identity'),
                    'routing_result_relative': record.get('routing_result_relative'),
                    'result_file': str(source.relative_to(run))}
            if set(station_results) != set(stations):
                raise ValueError('round does not contain exactly all expected station results')
            if any(row.get('result_scope') == 'accepted_joint_state' for row in station_results.values()):
                receipt = _read(path / 'joint_execution_receipt.json')
                vector = {sid: row['effective_parameters'] for sid,row in station_results.items()}
                if (not all(row.get('result_scope') == 'accepted_joint_state' for row in station_results.values())
                        or receipt.get('status') != 'consistent' or receipt.get('round_id') != rid
                        or receipt.get('vector_sha256') != hashlib.sha256(json.dumps(vector,sort_keys=True,allow_nan=False).encode()).hexdigest()):
                    raise ValueError('joint accepted vector receipt missing/mismatched')
            values = [station_results[sid]['NSE'] for sid in stations]
            mean = math.fsum(values) / len(values)
            if not math.isfinite(mean):
                raise ValueError('arithmetic mean NSE is not finite')
            valid.append({'round_id': rid, 'phase': meta.get('phase'), 'mean_nse': mean,
                          'station_results': station_results, 'round_dir': str(path.relative_to(run))})
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            invalid.append({'round_id': rid, 'reason': str(exc)})
    selected = max(valid, key=lambda row: (row['mean_nse'], -row['round_id'])) if valid else None
    complete = selected is not None and not missing and not invalid and not unexpected
    result = {'schema_version': 1, 'run_root': str(run),
        'selection_rule': 'highest arithmetic mean of finite successful selected station NSE across complete main rounds; exact tie earliest round',
        'selection_uses_full_precision': True, 'exploration_included': False, 'unselected_mc_included': False,
        'status': 'complete' if complete else ('incomplete_selection' if selected else 'no_valid_round'),
        'selection_complete': complete, 'provisional': not complete, 'expected_rounds': expected_rounds,
        'expected_stations': stations, 'rounds_discovered': sorted(directories),
        'valid_round_count': len(valid), 'missing_rounds': missing, 'invalid_rounds': invalid, 'unexpected_rounds': unexpected,
        'round_scores': [{'round_id': row['round_id'], 'phase': row['phase'], 'mean_nse': row['mean_nse']} for row in valid],
        'selected_round_id': selected['round_id'] if selected else None,
        'mean_nse': selected['mean_nse'] if selected else None,
        'selected_round_dir': selected['round_dir'] if selected else None,
        'station_results': selected['station_results'] if selected else {}, **identity}
    return result


def _matching_record(run, selection, sid, index):
    chosen = selection['station_results'][sid]
    rid, candidate = selection['selected_round_id'], chosen['winning_candidate_id']
    matches = []
    for encoded, attempt in index.items():
        key = json.loads(encoded)
        identity = chosen.get('execution_identity')
        if len(key) != 4 or key[0] != sid:
            continue
        if identity:
            if key[1:] != [identity['run_id'], identity['candidate_id'], identity['phase_tag']]:
                continue
        elif not str(key[3]).startswith(f'round_{rid}_') or (candidate is not None and key[2] != candidate):
            continue
        record_path = _safe_path(run / 'native_audit', f'candidates/{attempt}/execution_record.json')
        record = _read(record_path)
        if (record.get('effective_parameters') == chosen['effective_parameters'] and
                record.get('metrics') == chosen['metrics']):
            if (record.get('attempt_id') != attempt or record.get('kind') != 'candidate' or
                    record.get('subbasin_id') != sid or record.get('run_id') != key[1] or
                    record.get('candidate_id') != key[2] or record.get('phase_tag') != key[3] or
                    record.get('ok') is not True or record.get('mode') != selection['mode']):
                raise RepresentativeError(f'{sid}: execution record identity/status mismatch', selection)
            matches.append((record_path, record))
    if len(matches) != 1:
        raise RepresentativeError(f'{sid}: expected one execution record matching selected parameters/metrics; found {len(matches)}', selection)
    return matches[0]


def _matching_round_day(run, selection, sid, record, artifact):
    explicit_source = selection['station_results'][sid].get('routing_result_relative')
    base = (_safe_path(run, explicit_source) if explicit_source else
            run / selection['selected_round_dir'] / 'routing_result' / sid)
    paths = [p for p in base.rglob('*') if p.is_file() and not p.name.startswith('._')
             and (p.name.endswith('.day') or p.name.endswith('.day.gz'))]
    if not explicit_source and selection['station_results'][sid]['has_mc_selection']:
        prefix = re.compile(r'^c' + re.escape(str(record['candidate_id'])) + r'(?:_|$)')
        paths = [p for p in paths if len(p.relative_to(base).parts) > 1 and prefix.match(p.relative_to(base).parts[0])]
    else:
        paths = [p for p in paths if len(p.relative_to(base).parts) == 1]
    matches = [p for p in paths if _sha(p, p.name.endswith('.gz')) == artifact['sha256']]
    if len(matches) != 1:
        raise RepresentativeError(f'{sid}: selected candidate routing day does not uniquely match its round export', selection)
    return matches[0]


def _cache_path(run, archived):
    """Relocate only recorded routing_cache paths within this copied run."""
    parts = Path(archived).parts
    if 'routing_cache' not in parts:
        raise RepresentativeError('Cached input origin is not inside routing_cache')
    return _safe_path(run, Path(*parts[parts.index('routing_cache'):]))


def audit_upstream_vector(config, station_results, records, mode='real'):
    """Compare actual archived upstream fields with the selected station vector.

    Identity uses configured source paths, never basin-name heuristics. Topology
    requires every configured ancestor exactly once for soil and routing.
    """
    result = {'status': 'not_applicable_simulation', 'consistent': None,
              'mismatches': [], 'unresolved_inputs': [], 'checked_upstream_links': 0,
              'scope': 'archived upstream fields versus selected station parameters; no independent model replay'}
    if mode != 'real':
        return result
    workers = {w['subbasin_id']: w for w in config.get('workers', [])}
    stations = set(station_results)
    if set(workers) != stations or set(records) != stations:
        result['unresolved_inputs'].append({'kind': 'configured_selected_record_station_sets_differ'})
    parents = {sid: set() for sid in workers}
    for sid, w in workers.items():
        for downstream in w.get('neighbors_downstream', []):
            if downstream not in parents:
                result['unresolved_inputs'].append({'kind': 'unknown_topology_station', 'station': downstream})
            else:
                parents[downstream].add(sid)

    def ancestors(sid, trail=()):
        if sid in trail:
            raise ValueError('cyclic configured topology')
        return set(parents.get(sid, ())).union(*(
            ancestors(upstream, trail + (sid,)) for upstream in parents.get(sid, ())))

    for sid, chosen in station_results.items():
        record = records.get(sid, {})
        try:
            expected = ancestors(sid)
        except ValueError as error:
            result['unresolved_inputs'].append({'station': sid, 'kind': str(error)})
            expected = set()
        for kind, field, keys in (('soil', 'upstream_soil', SOIL_PARAMETERS),
                                  ('routing', 'upstream_routing', ROUTING_PARAMETERS)):
            entries = record.get(field)
            if not isinstance(entries, list):
                result['unresolved_inputs'].append({'station': sid, 'kind': field + '_missing'})
                continue
            seen = []
            for entry in entries:
                if kind == 'soil':
                    path = entry.get('soil_base')
                    matches = [u for u, w in workers.items() if path and w.get('soil_base') == path]
                    actual = entry.get('params', {})
                else:
                    path = entry.get('direc_path')
                    matches = [u for u, w in workers.items() if path and w.get('rout_template_dir')
                               and Path(path).parent == Path(w['rout_template_dir'])]
                    actual = entry
                if len(matches) != 1:
                    result['unresolved_inputs'].append({'station': sid, 'kind': kind,
                        'source_path': path, 'reason': 'upstream source is not uniquely configured'})
                    continue
                upstream = matches[0]
                seen.append(upstream)
                if upstream not in stations:
                    result['unresolved_inputs'].append({'station': sid, 'upstream': upstream,
                                                        'kind': 'upstream_station_result_missing'})
                    continue
                target = station_results[upstream]['effective_parameters']
                unknown = [key for key in keys if not _finite(actual.get(key)) or not _finite(target.get(key))]
                if unknown:
                    result['unresolved_inputs'].append({'station': sid, 'upstream': upstream,
                                                        'kind': kind, 'missing_or_nonfinite_parameters': unknown})
                    continue
                result['checked_upstream_links'] += 1
                difference = {key: {'executed': actual[key], 'selected_upstream': target[key]}
                              for key in keys if actual[key] != target[key]}
                if difference:
                    result['mismatches'].append({'downstream': sid, 'upstream': upstream,
                                                 'kind': kind, 'parameters': difference})
            if set(seen) != expected or len(seen) != len(set(seen)):
                result['unresolved_inputs'].append({'station': sid, 'kind': kind,
                    'expected_configured_ancestors': sorted(expected), 'recorded_upstream_stations': seen})
    result['status'] = ('unverifiable' if result['unresolved_inputs'] else
                        'inconsistent' if result['mismatches'] else 'consistent')
    result['consistent'] = (None if result['unresolved_inputs'] else not result['mismatches'])
    return result


def select_final_confirmation(run_root, expected_rounds, expected_stations=None):
    """Select the fixed terminal round, never a second score optimization."""
    result = select_representative_round(run_root, expected_rounds, expected_stations)
    if not result['selection_complete']:
        raise RepresentativeError('Terminal confirmation requires complete main rounds', result)
    rid = expected_rounds if expected_rounds is not None else max(result['rounds_discovered'])
    run = Path(result['run_root'])
    round_dir = next(p for p in run.iterdir() if p.is_dir() and ROUND_PATTERN.fullmatch(p.name)
                     and int(ROUND_PATTERN.fullmatch(p.name).group(1)) == rid)
    stations = {}
    for source in sorted((round_dir / 'results').glob('*.json')):
        if source.name.startswith('._'):
            continue
        record = _read(source); selected = record['result']; sid = record['subbasin_id']
        stations[sid] = {'subbasin_id': sid, 'NSE': selected['metrics']['NSE'],
            'metrics': selected['metrics'], 'effective_parameters': selected['params'],
            'winning_candidate_id': record.get('winning_candidate_id'),
            'has_mc_selection': 'mc_detail' in record, 'result_file': str(source.relative_to(run)),
            'result_scope': record.get('result_scope'), 'execution_identity': record.get('execution_identity'),
            'routing_result_relative': record.get('routing_result_relative')}
    result.update(selected_round_id=rid, selected_round_dir=str(round_dir.relative_to(run)),
                  mean_nse=math.fsum(stations[sid]['NSE'] for sid in sorted(stations))/len(stations),
                  station_results=stations,
                  selection_rule='fixed terminal force-best confirmation round; not selected by score',
                  result_kind='terminal_confirmation')
    return result


def validate_terminal_confirmation(run_root, selection, records, vector_audit):
    """Require trace identity plus fresh native evidence for a physical claim."""
    run = Path(run_root)
    stations = set(selection['expected_stations'])
    directory = run / selection['selected_round_dir'] / 'process_trace'
    traces = []
    for path in sorted(directory.glob('*.json')):
        record = _read(path)
        if record.get('event') == 'candidate_process':
            traces.append((path, record))
    if len(traces) != len(stations) or {r.get('subbasin_id') for _, r in traces} != stations:
        raise RepresentativeError('Terminal confirmation needs exactly one force-best candidate per station', selection)
    for _, record in traces:
        sid = record['subbasin_id']; selected = selection['station_results'][sid]
        if (record.get('round_id') != selection['selected_round_id'] or record.get('branch') != 'force_best'
                or record.get('ok') is not True or record.get('effective_params') != selected['effective_parameters']
                or record.get('metrics') != selected['metrics']
                or (selection['mode'] == 'real' and record.get('candidate_id') != records[sid]['candidate_id'])):
            raise RepresentativeError(f'{sid}: terminal force-best trace differs from selected execution', selection)
    info = {'status': 'verified_simulation_only', 'terminal_round': selection['selected_round_id'],
            'force_best_all_stations': True, 'fresh_native_calls_verified': False,
            'upstream_vector_consistent': None, 'native_event_counts': {}}
    events = []
    if selection['mode'] == 'real':
        if vector_audit['status'] != 'consistent' or vector_audit['consistent'] is not True:
            raise RepresentativeError('Terminal confirmation upstream vector is inconsistent or unverifiable', selection)
        raw_events = [json.loads(line) for line in (run/'native_audit/events.jsonl').read_text().splitlines()
                      if line.strip()]
        for sid, record in records.items():
            matched = [e for e in raw_events if e.get('attempt_id') == record['attempt_id']]
            counts = {kind: sum(e.get('event') == kind for e in matched)
                      for kind in ('start', 'vic_launched', 'routing_launched', 'end')}
            if any(value != 1 for value in counts.values()):
                raise RepresentativeError(f'{sid}: terminal execution lacks unique fresh VIC/routing events', selection)
            required = [e for e in matched if e.get('event') in counts]
            if any(e.get('subbasin_id') != sid or e.get('kind') != 'candidate' for e in required):
                raise RepresentativeError(f'{sid}: terminal native event identity mismatch', selection)
            start = next(e for e in required if e['event'] == 'start')
            end = next(e for e in required if e['event'] == 'end')
            if (any(start.get(key) != record.get(key) for key in
                    ('run_id', 'candidate_id', 'phase_tag', 'mode', 'upstream_soil', 'upstream_routing'))
                    or end.get('ok') is not True or end.get('metrics') != record.get('metrics')
                    or end.get('effective_parameters') != record.get('effective_parameters')):
                raise RepresentativeError(f'{sid}: terminal start/end event disagrees with execution record', selection)
            events.extend(matched); info['native_event_counts'][sid] = counts
        info.update(status='verified_real', fresh_native_calls_verified=True, upstream_vector_consistent=True)
    return info, [p for p, _ in traces], events


def export_representative(run_root, output_dir, expected_rounds=None, expected_stations=None,
                          online_best_global=None):
    selection = select_representative_round(run_root, expected_rounds, expected_stations)
    selection['result_kind'] = 'online_best_round_aggregate'
    return _export_selected(run_root, output_dir, selection, online_best_global)


def export_final_confirmation(run_root, output_dir, expected_rounds, expected_stations=None,
                              online_best_global=None):
    selection = select_final_confirmation(run_root, expected_rounds, expected_stations)
    return _export_selected(run_root, output_dir, selection, online_best_global)


def _export_selected(run_root, output_dir, selection, online_best_global=None):
    """Atomically publish a new verified export, or raise without selecting another round.

    Raises RepresentativeError for mismatched/missing selected native evidence.
    Simulated runs copy selection/results but explicitly omit native verification.
    The destination must not exist. Nothing in the source run is rewritten.
    """
    run = Path(run_root).expanduser().resolve(strict=True)
    out = Path(output_dir).expanduser().absolute()
    if out.exists():
        raise FileExistsError(f'Representative export already exists: {out}')
    if selection['selected_round_id'] is None:
        raise RepresentativeError('No complete finite successful main round is available', selection)
    if online_best_global is not None:
        selection['online_best_global_j_total'] = online_best_global
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = out.parent / ('.' + out.name + '.export.lock')
    lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    staging = None
    try:
        os.close(lock_fd)
        if out.exists():
            raise FileExistsError(f'Representative export already exists: {out}')
        staging = Path(tempfile.mkdtemp(prefix='.' + out.name + '.staging_', dir=out.parent))
        copied = {}

        def copy_source(source, *, durable_cache_origin=False):
            source = source.resolve()
            if not source.is_relative_to(run):
                raise RepresentativeError('Attempted to copy a file outside the source run', selection)
            source_relative = str(source.relative_to(run))
            before = _sha(source)
            # Cache origins may live under disposable current_run directories.
            # Freeze a separate content-addressed copy outside that cleanup namespace.
            relative = (str(Path('cache_origin_evidence') / before / source.name)
                        if durable_cache_origin else source_relative)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative not in copied:
                shutil.copyfile(source, destination)
                if _sha(destination) != before or _sha(source) != before:
                    raise RepresentativeError(f'Source changed while copying: {relative}', selection)
                copied[relative] = {'sha256': before, 'bytes': destination.stat().st_size, 'source': source_relative}
            return relative

        rid = selection['selected_round_id']
        round_path = run / selection['selected_round_dir']
        copy_source(round_path / 'round_meta.json')
        if (round_path / 'joint_execution_receipt.json').exists():
            copy_source(round_path / 'joint_execution_receipt.json')
        context_path = round_path / 'process_trace/round_context.json'
        if context_path.is_file():
            context = _read(context_path)
            station_rows = context.get('worker_results', [])
            by_sid = {r.get('subbasin_id'): r for r in station_rows}
            if len(by_sid) != len(station_rows) or set(by_sid) != set(selection['expected_stations']):
                raise RepresentativeError('Selected round context station set mismatch', selection)
            for sid, chosen in selection['station_results'].items():
                row = by_sid[sid]
                if row.get('best_params') != chosen['effective_parameters'] or row.get('metrics') != chosen['metrics'] or row.get('ok') is not True:
                    raise RepresentativeError(f'{sid}: selected round context disagrees with station result', selection)
            copy_source(context_path)
        for name in ('provenance/config.json', 'provenance/manifest.json', 'provenance/input_fingerprint.json', 'run_manifest.json'):
            source = run / name
            if source.is_file():
                copy_source(source)
        real = selection['mode'] == 'real'
        index_path = run / 'native_audit/candidate_index.json'
        index = _read(index_path) if real else {}
        evidence = {}
        execution_records = {}
        for sid, chosen in selection['station_results'].items():
            copy_source(run / chosen['result_file'])
            if not real:
                continue
            record_path, record = _matching_record(run, selection, sid, index)
            execution_records[sid] = record
            copy_source(record_path)
            selected_days = []
            for artifact in record.get('artifacts', []):
                saved = _safe_path(run / 'native_audit', artifact['saved'])
                compressed = artifact.get('compression') == 'gzip'
                if not saved.is_file() or _sha(saved, compressed) != artifact['sha256']:
                    raise RepresentativeError(f'{sid}: archived artifact hash mismatch/missing: {artifact["saved"]}', selection)
                copy_source(saved)
                if saved.name.endswith('.day') or saved.name.endswith('.day.gz'):
                    round_day = _matching_round_day(run, selection, sid, record, artifact)
                    selected_days.append({'native_artifact': str(saved.relative_to(run)),
                        'round_artifact': copy_source(round_day), 'uncompressed_sha256': artifact['sha256']})
            if not selected_days:
                raise RepresentativeError(f'{sid}: no verified routed daily result', selection)
            cache = record.get('cache_status') or {}
            cached_origins = []
            if cache.get('status') == 'validated':
                references = [{'path': cache['manifest'], 'sha256': cache['manifest_sha256']}]
                references.extend(cache.get('effective_input_origin', {}).values())
                for reference in references:
                    source = _cache_path(run, reference['path'])
                    if not source.is_file() or _sha(source) != reference['sha256']:
                        raise RepresentativeError(f'{sid}: cache origin evidence missing/changed', selection)
                    cached_origins.append(copy_source(source, durable_cache_origin=True))
            evidence[sid] = {'attempt_id': record['attempt_id'], 'candidate_id': record['candidate_id'],
                'phase_tag': record['phase_tag'], 'execution_record': str(record_path.relative_to(run)),
                'routing_days': selected_days, 'cache_origin_files': cached_origins}
        config, _ = _identity(run)
        vector_audit = audit_upstream_vector(config, selection['station_results'], execution_records, selection['mode'])
        selection['result_export_protocol'] = RESULT_EXPORT_PROTOCOL
        selection['upstream_vector_audit'] = vector_audit
        if real and any(row.get('result_scope') == 'accepted_joint_state' for row in selection['station_results'].values()):
            if vector_audit.get('status') != 'consistent' or vector_audit.get('consistent') is not True:
                raise RepresentativeError('Accepted joint result has inconsistent/unverifiable native upstream inputs', selection)
        if selection['result_kind'] == 'terminal_confirmation':
            info, traces, events = validate_terminal_confirmation(run, selection, execution_records, vector_audit)
            selection['terminal_confirmation'] = info
            for path in traces:
                copy_source(path)
            if real:
                (staging / 'native_audit').mkdir(exist_ok=True)
                (staging / 'native_audit/selected_execution_events.jsonl').write_text(
                    ''.join(json.dumps(e, ensure_ascii=False, allow_nan=False) + '\n' for e in events), encoding='utf-8')
        selection['native_artifact_verification'] = 'verified' if real else 'not_applicable_simulation'
        selection['audit_complete'] = True
        selection['execution_evidence'] = evidence
        selection['reproduction_note'] = ('Each selected station execution retains its own upstream_soil/upstream_routing and saved physical inputs. '
            'The station parameter table alone is not asserted to reproduce every station as one combined vector; '
            'historical accepted upstream centers can differ from upstream candidates in the selected round.')
        if any(row.get('result_scope') == 'accepted_joint_state' for row in selection['station_results'].values()):
            selection['reproduction_note'] = ('One accepted joint parameter vector; rejected station proposals are separate. '
                'Each station score links to its actual execution, including same-background center references or reused headwater evidence. '
                'This export audits archived evidence and does not launch a new model run.')
        if selection['result_kind'] == 'terminal_confirmation':
            selection['reproduction_note'] = (
                'Fixed terminal force-best round. Archived station inputs form one consistent upstream parameter vector '
                'and each station has fresh VIC and routing execution evidence; this export did not rerun the models.'
                if real else 'Simulation-only fixed terminal force-best round; no physical execution or physical vector consistency is claimed.')
        _write(staging / 'selection.json', selection)
        _write(staging / 'effective_parameters.json', {sid: row['effective_parameters'] for sid, row in selection['station_results'].items()})
        selected_index = {key: value for key, value in index.items() if value in {row['attempt_id'] for row in evidence.values()}}
        if real:
            (staging / 'native_audit').mkdir(exist_ok=True)
            _write(staging / 'native_audit/selected_candidate_index.json', selected_index)
        files = {str(p.relative_to(staging)): {'sha256': _sha(p), 'bytes': p.stat().st_size}
                 for p in sorted(staging.rglob('*')) if p.is_file()}
        manifest = {'schema_version': 1, 'source_run': str(run), 'output_dir': str(out),
            'result_export_protocol': RESULT_EXPORT_PROTOCOL, 'result_kind': selection['result_kind'],
            'upstream_vector_audit': vector_audit,
            'status': selection['status'], 'selection_complete': selection['selection_complete'],
            'provisional': selection['provisional'], 'selected_round_id': rid, 'mean_nse': selection['mean_nse'],
            'selection_rule': selection['selection_rule'], 'native_artifact_verification': selection['native_artifact_verification'],
            'audit_complete': True, 'test_only': selection['test_only'],
            'online_best_global_j_total': selection['online_best_global_j_total'],
            'reproduction_note': selection['reproduction_note'],
            'copied_source_paths': {relative: entry['source'] for relative, entry in copied.items()},
            'files': files, 'file_hash_scope': 'stored bytes; manifest excludes itself; daily decompressed hashes are in selection.json'}
        if 'terminal_confirmation' in selection:
            manifest['terminal_confirmation'] = selection['terminal_confirmation']
        _write(staging / 'manifest.json', manifest)
        if out.exists():
            raise FileExistsError(f'Representative export already exists: {out}')
        os.rename(staging, out)
        staging = None
        return manifest
    except RepresentativeError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RepresentativeError(f'Chosen-round export failed: {type(exc).__name__}: {exc}', selection) from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging)
        lock.unlink(missing_ok=True)
