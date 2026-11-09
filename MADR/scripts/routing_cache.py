"""Authenticated, per-station VIC-flux caches for the routing-only phase.

No cache key contains routing velocity/diffusivity: those remain tunable.
Digests are cached only while dev/inode/size/mtime_ns/ctime_ns are unchanged.
Each lookup enumerates dependencies again, so added/deleted forcing files and
ordinary in-place edits invalidate the cache rather than silently reusing it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import shlex
import shutil
import threading
import uuid

from .execution_protocol import SOIL_KEYS
from .native_audit import atomic_json, sha256_file


class CacheMismatchError(RuntimeError):
    pass


class ContentFingerprinter:
    def __init__(self):
        self.memo = {}
        self.lock = threading.RLock()

    def file(self, path):
        p = Path(path).resolve(strict=True)
        if not p.is_file():
            raise ValueError('Cache dependency is not a file: ' + str(p))
        st = p.stat()
        identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        with self.lock:
            previous = self.memo.get(str(p))
            if previous is None or previous[0] != identity:
                digest = sha256_file(p)
                after = p.stat()
                if identity != (after.st_dev, after.st_ino, after.st_size,
                                after.st_mtime_ns, after.st_ctime_ns):
                    raise CacheMismatchError('Input changed while hashing: ' + str(p))
                self.memo[str(p)] = (identity, digest)
            else:
                digest = previous[1]
        return {'path': str(p), 'bytes': st.st_size, 'sha256': digest}


def _content_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def flux_inputs(cfg, params, upstream_soil_info, fingerprinter):
    """All VIC inputs referenced by the actual global template plus soil state."""
    p = Path(cfg.global_base).resolve(strict=True)
    dependencies = {}
    forcing_groups = {}
    # Output directives and SOIL are rewritten by the runner; never digest an
    # old output directory as if it were a model input.
    outputs = {'RESULT_DIR', 'OUTFILE', 'STATENAME', 'STATEFILE', 'SOIL'}
    for line in p.read_text().splitlines():
        values = shlex.split(line, comments=True)
        if len(values) < 2:
            continue
        key, tokens = values[0].upper(), values[1:]
        if key in outputs:
            continue
        if key in ('FORCING1', 'FORCING2'):
            token = tokens[0]
            if token.upper() in ('FALSE', 'NONE', 'NULL'):
                continue
            prefix = Path(token)
            if not prefix.is_absolute():
                raise CacheMismatchError('Cache requires an absolute forcing prefix: ' + token)
            matches = sorted(q for q in prefix.parent.glob(prefix.name + '*') if q.is_file())
            if not matches:
                raise CacheMismatchError('No forcing files for prefix: ' + token)
            forcing_groups[key] = [fingerprinter.file(q) for q in matches]
            continue
        for token in tokens:
            candidate = Path(token)
            if candidate.is_absolute():
                if not candidate.is_file():
                    raise CacheMismatchError('Missing referenced VIC input: ' + token)
                dependencies[str(candidate.resolve())] = fingerprinter.file(candidate)
            elif (p.parent / candidate).is_file():
                # Relative file names would resolve against changing native
                # working directories, so require explicit staging first.
                raise CacheMismatchError('Cache requires absolute VIC input paths: ' + token)
    if not forcing_groups:
        raise CacheMismatchError('Global template has no usable forcing prefix')
    upstream = []
    for info in upstream_soil_info or []:
        upstream.append({'soil_base': fingerprinter.file(info.soil_base),
                         'effective_soil': {k: info.params[k] for k in SOIL_KEYS}})
    return {
        'protocol': 'vic-routing-flux-cache-v1',
        'soil_base': fingerprinter.file(cfg.soil_base),
        'global_template': fingerprinter.file(cfg.global_base),
        'vic_executable': fingerprinter.file(cfg.vic_exe),
        'dependencies': [dependencies[k] for k in sorted(dependencies)],
        'forcing': forcing_groups,
        'effective_soil': {k: params[k] for k in SOIL_KEYS},
        'upstream_soil_in_application_order': upstream,
        'simulation_years': [int(cfg.start_year), int(cfg.end_year)],
        'evaluation_years': [cfg.eval_start_year, cfg.eval_end_year],
        'flux_prefix': cfg.flux_prefix,
    }


def expected_flux_names(soil_path, flux_prefix):
    cells = []
    for line in Path(soil_path).read_text().splitlines():
        fields = line.split()
        if not fields or fields[0].startswith('#'):
            continue
        if len(fields) < 25:
            raise CacheMismatchError('Malformed active soil file row')
        if int(fields[0]) != 1:
            continue
        cells.append(f'{flux_prefix}{float(fields[2]):.4f}_{float(fields[3]):.4f}')
    if not cells or len(cells) != len(set(cells)):
        raise CacheMismatchError('Soil grid is empty or has duplicate flux coordinates')
    return sorted(cells)


def validate_complete_flux(directory, soil_path, cfg, fingerprinter):
    root = Path(directory)
    expected = expected_flux_names(soil_path, cfg.flux_prefix)
    present = sorted(p.name for p in root.iterdir() if p.is_file())
    if present != expected:
        raise CacheMismatchError('Flux grid coverage does not match active soil cells')
    start, end = dt.date(cfg.start_year, 1, 1), dt.date(cfg.end_year, 12, 31)
    files = []
    for name in expected:
        path = root / name
        next_day = start
        with path.open() as stream:
            for line in stream:
                row = line.split()
                if len(row) < 7:
                    raise CacheMismatchError('Incomplete preprocessed flux row: ' + name)
                date = dt.date(int(row[0]), int(row[1]), int(row[2]))
                if date != next_day or date > end:
                    raise CacheMismatchError('Flux is not a complete ordered daily series: ' + name)
                if not all(math.isfinite(float(v)) for v in row[4:7]):
                    raise CacheMismatchError('Nonfinite flux: ' + name)
                next_day += dt.timedelta(days=1)
        if next_day != end + dt.timedelta(days=1):
            raise CacheMismatchError('Truncated flux series: ' + name)
        files.append({'name': name, **fingerprinter.file(path)})
    return files


class RoutingFluxCache:
    def __init__(self, output_root):
        self.root = Path(output_root) / 'routing_cache'
        self.root.mkdir(parents=True, exist_ok=True)
        self.fp = ContentFingerprinter()
        self.lock = threading.RLock()
        index = self.root / 'active.json'
        self.active = json.loads(index.read_text()) if index.exists() else {}

    def _save(self):
        atomic_json(self.root / 'active.json', self.active)

    def deactivate(self, sid):
        with self.lock:
            self.active.pop(sid, None)
            self._save()

    def clear(self, remove_flux=True):
        with self.lock:
            old = dict(self.active)
            self.active = {}
            self._save()
        removed = []
        if remove_flux:
            for entry in old.values():
                manifest_path = Path(entry['manifest'])
                if manifest_path.is_file():
                    manifest = json.loads(manifest_path.read_text())
                    flux = manifest_path.parent / manifest['flux_relative_path']
                    if flux.is_dir() and self.root.resolve() in flux.resolve().parents:
                        shutil.rmtree(flux)
                        removed.append(str(flux))
        return {'status': 'cleared', 'basins': sorted(old), 'removed_flux_directories': removed}

    def verify(self, sid, cfg, params, upstream_soil_info):
        with self.lock:
            entry = self.active.get(sid)
        if entry is None:
            raise CacheMismatchError('No active routing cache for ' + sid)
        inputs = flux_inputs(cfg, params, upstream_soil_info, self.fp)
        key = _content_key(inputs)
        if key != entry['key']:
            raise CacheMismatchError('Routing cache inputs changed for ' + sid)
        path = Path(entry['manifest'])
        if not path.is_file() or self.fp.file(path)['sha256'] != entry['manifest_sha256']:
            raise CacheMismatchError('Routing cache manifest missing or changed for ' + sid)
        manifest = json.loads(path.read_text())
        if not manifest.get('complete') or manifest['key'] != key:
            raise CacheMismatchError('Routing cache was not completed for ' + sid)
        flux = path.parent / manifest['flux_relative_path']
        names = sorted(p.name for p in flux.iterdir() if p.is_file()) if flux.is_dir() else []
        if names != sorted(row['name'] for row in manifest['flux_files']):
            raise CacheMismatchError('Routing cache flux files missing or added for ' + sid)
        for row in manifest['flux_files']:
            if self.fp.file(flux / row['name'])['sha256'] != row['sha256']:
                raise CacheMismatchError('Routing cache flux content changed for ' + sid)
        origins = {}
        for key_name, row in manifest['effective_inputs'].items():
            input_path = path.parent / row['relative_path']
            if self.fp.file(input_path)['sha256'] != row['sha256']:
                raise CacheMismatchError('Cached effective input changed: ' + str(input_path))
            origins[key_name] = {'path': str(input_path), 'sha256': row['sha256']}
        return {'status': 'validated', 'subbasin_id': sid, 'key': key,
                'manifest': str(path), 'manifest_sha256': entry['manifest_sha256'],
                'flux_dir': str(flux), 'effective_input_origin': origins}

    def prepare(self, sid, cfg, params, upstream_soil_info, event, run_id=99999):
        from . import vic_runner
        inputs = flux_inputs(cfg, params, upstream_soil_info, self.fp)
        key = _content_key(inputs)
        if sid in self.active:
            try:
                status = self.verify(sid, cfg, params, upstream_soil_info)
                return {**status, 'status': 'reused'}
            except CacheMismatchError:
                # Explicit preparation can rebuild. Ordinary candidate lookup
                # never rebuilds or hides a mismatch.
                self.deactivate(sid)
        build = self.root / sid / ('build_' + uuid.uuid4().hex)
        build.mkdir(parents=True, exist_ok=False)
        runtime = vic_runner.RuntimeConfig(
            **{k: getattr(cfg, k) for k in vic_runner.RuntimeConfig.__dataclass_fields__
               if k not in ('runtime_event', 'base_output_dir')},
            base_output_dir=str(build), runtime_event=event)
        try:
            ok, flux_dir = vic_runner.run_vic_flux_only(
                runtime, run_id, params, 'cache_preparation', str(build / 'preparation.log'),
                upstream_soil_info=upstream_soil_info)
            if not ok:
                raise CacheMismatchError('VIC cache preparation failed for ' + sid)
            current = Path(runtime.current_dir)
            soil = current / 'SOIL_PARAM_COMPLETE.txt'
            files = validate_complete_flux(flux_dir, soil, cfg, self.fp)
            # Inputs must remain stable for the complete preparation call.
            if _content_key(flux_inputs(cfg, params, upstream_soil_info, self.fp)) != key:
                raise CacheMismatchError('VIC inputs changed during cache preparation')
            effective = {}
            for name in ('SOIL_PARAM_COMPLETE.txt', 'global_param.txt'):
                effective[name] = {'relative_path': str((current / name).relative_to(build)),
                                   'sha256': self.fp.file(current / name)['sha256']}
            manifest = {'complete': True, 'key': key, 'inputs': inputs,
                        'flux_relative_path': str(Path(flux_dir).relative_to(build)),
                        'flux_files': [{k: v for k, v in row.items() if k != 'path'} for row in files],
                        'effective_inputs': effective}
            raw = current / 'vic_result'
            if raw.is_dir():
                shutil.rmtree(raw)
            destination = build.with_name(key + '_' + uuid.uuid4().hex[:8])
            build.rename(destination)
            path = destination / 'manifest.json'
            atomic_json(path, manifest)
            with self.lock:
                self.active[sid] = {'key': key, 'manifest': str(path),
                                    'manifest_sha256': self.fp.file(path)['sha256']}
                self._save()
            event('workspace_ready', current_dir=str(destination / Path(runtime.current_dir).relative_to(build)))
            return {**self.verify(sid, cfg, params, upstream_soil_info), 'status': 'prepared'}
        except Exception:
            self.deactivate(sid)
            if build.exists():
                # Preserve small evidence, never a failed multi-GB flux tree.
                for child in build.rglob('vic_result'):
                    if child.is_dir():shutil.rmtree(child)
                for child in build.rglob('vic_for_routing'):
                    if child.is_dir():shutil.rmtree(child)
            raise
