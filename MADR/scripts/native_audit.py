"""Small, append-only native-run evidence and actual process-start accounting."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil
import threading
import time
import uuid


def json_safe(value):
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(json_safe(value), ensure_ascii=False, indent=2,
                                    allow_nan=False) + '\n')
    temporary.replace(path)


class NativeAudit:
    """One object per graph run; safe for its parallel station workers.

    Launch counters advance only after Popen has returned a process. An
    evaluation attempt is not a process launch and a successful evaluation is
    not used as a proxy for either VIC or routing calls.
    """
    COUNTERS = ('candidate_evaluations', 'candidate_successes', 'candidate_failures',
                'actual_vic_launches', 'routing_launches',
                'cache_preparation_attempts', 'cache_preparation_successes',
                'cache_preparation_failures', 'cache_hits', 'simulated_cache_hits',
                'center_reference_cache_hits')

    def __init__(self, output_root, enabled=True, retain_candidates=True):
        self.root = Path(output_root) / 'native_audit'
        self.enabled = bool(enabled)
        self.retain_candidates = bool(retain_candidates)
        self.lock = threading.RLock()
        self.counts = dict.fromkeys(self.COUNTERS, 0)
        self.candidate_index = {}
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
            old = self.root / 'accounting.json'
            if old.is_file():
                loaded = json.loads(old.read_text())
                self.counts.update({k: int(loaded['counts'].get(k, 0)) for k in self.COUNTERS})
            index = self.root / 'candidate_index.json'
            if index.is_file():
                self.candidate_index = json.loads(index.read_text())
            self._save_counts()

    def _save_counts(self):
        if self.enabled:
            atomic_json(self.root / 'accounting.json', {
                'counts': self.counts,
                'definitions': {
                    'candidate_evaluations': 'All executor.run attempts, including prelaunch rejection and simulated mode.',
                    'actual_vic_launches': 'Popen returned a VIC process; includes cache preparation and failed model runs.',
                    'routing_launches': 'Popen returned a routing process; includes failed model runs.',
                    'cache_preparation_attempts': 'Explicit preparation/reuse attempts, separate from candidate evaluations.',
                    'cache_hits': 'Real candidate evaluations using an authenticated flux cache.',
                    'simulated_cache_hits': 'Simulated candidates using simulated cache state; no native launch.',
                    'center_reference_cache_hits': 'Authenticated existing evaluations used as center references; no executor.run or native launch.',
                },
            })

    def event(self, run, event, **details):
        with self.lock:
            if event == 'vic_launched':
                self.counts['actual_vic_launches'] += 1
            elif event == 'routing_launched':
                self.counts['routing_launches'] += 1
            elif event in ('cache_hit', 'simulated_cache_hit'):
                self.counts['cache_hits' if event == 'cache_hit' else 'simulated_cache_hits'] += 1
            elif event == 'center_reference_reused':
                self.counts['center_reference_cache_hits'] += 1
            if event == 'workspace_ready':
                run['current_dir'] = details['current_dir']
            row = {'event': event, 'attempt_id': run['attempt_id'],
                   'kind': run['kind'], 'subbasin_id': run['metadata'].get('subbasin_id'),
                   'time_utc': datetime.now(timezone.utc).isoformat(), **details}
            if self.enabled:
                with (self.root / 'events.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(json_safe(row), ensure_ascii=False, allow_nan=False) + '\n')
                    stream.flush()
            self._save_counts()

    def start(self, kind, **metadata):
        run = {'attempt_id': uuid.uuid4().hex, 'kind': kind,
               'started': time.monotonic(), 'metadata': json_safe(metadata)}
        with self.lock:
            if kind == 'candidate':
                key = json.dumps([metadata.get(k) for k in
                                  ('subbasin_id', 'run_id', 'candidate_id', 'phase_tag')])
                if key in self.candidate_index:
                    raise RuntimeError('Candidate identity already attempted; use an explicit new run/attempt identity')
                self.candidate_index[key] = run['attempt_id']
                if self.enabled:
                    atomic_json(self.root / 'candidate_index.json', self.candidate_index)
            self.counts['candidate_evaluations' if kind == 'candidate' else 'cache_preparation_attempts'] += 1
            self.event(run, 'start', **metadata)
        return run

    def callback(self, run):
        return lambda event, **details: self.event(run, event, **details)

    def _archive(self, run):
        if not (self.enabled and self.retain_candidates):
            return []
        target = self.root / 'candidates' / run['attempt_id']
        target.mkdir(parents=True, exist_ok=True)
        current = Path(run['current_dir']) if run.get('current_dir') else None
        manifest = []
        if current and current.is_dir():
            small = [current / 'SOIL_PARAM_COMPLETE.txt', current / 'global_param.txt',
                     current / 'vic_stderr.log']  # bounded failure tail; success deletes it
            routing = current / 'rout'
            if routing.is_dir():
                small.extend(p for p in routing.iterdir()
                             if p.is_file() and not p.is_symlink() and not p.name.endswith('.uh_s'))
            for source in small:
                if not source.is_file() or source.is_symlink():
                    continue
                relative = source.relative_to(current)
                row = {'source': str(source), 'relative_path': str(relative),
                       'bytes': source.stat().st_size, 'sha256': sha256_file(source)}
                if source.stat().st_size <= 16 * 1024 * 1024:
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                    row['saved'] = str(destination.relative_to(self.root))
                else:
                    row['not_copied'] = 'Above small-input retention limit; source and content hash retained.'
                manifest.append(row)
            flow_root = current / 'routing_result'
            if flow_root.is_dir():
                for source in sorted(flow_root.glob('*.day')):
                    if source.is_symlink() or not source.is_file():
                        continue
                    destination = target / 'routing_result' / (source.name + '.gz')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source.open('rb') as original, gzip.open(destination, 'wb') as compressed:
                        shutil.copyfileobj(original, compressed, length=1024 * 1024)
                    manifest.append({'source': str(source), 'sha256': sha256_file(source),
                                     'saved': str(destination.relative_to(self.root)),
                                     'compression': 'gzip', 'bytes': source.stat().st_size})
        return manifest

    def finish(self, run, ok, metrics=None, error=None, **details):
        if run.get('finished'):
            raise RuntimeError('Native attempt was already finalized')
        artifacts = self._archive(run)
        record = {'attempt_id': run['attempt_id'], 'kind': run['kind'],
                  **run['metadata'], 'ok': bool(ok), 'metrics': metrics or {},
                  'error': error, 'duration_sec': time.monotonic() - run['started'],
                  'artifacts': artifacts, **details}
        with self.lock:
            key = ('candidate_' if run['kind'] == 'candidate' else 'cache_preparation_')
            self.counts[key + ('successes' if ok else 'failures')] += 1
            if self.enabled:
                atomic_json(self.root / 'candidates' / run['attempt_id'] / 'execution_record.json', record)
            self.event(run, 'end' if ok else 'failure', ok=ok, metrics=metrics or {}, error=error, **details)
            run['finished'] = True
        return json_safe(record)
