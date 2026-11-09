"""Input binding helpers for the Linux launcher."""
from __future__ import annotations
from pathlib import Path
import shlex
from linux_common import read_json, sha256, PHYSICAL_KEYS
def binding_rows(path, expected_stations=None):
    """Accept a team config or a prepared staged_manifest.json."""
    d = read_json(path)
    if isinstance(d.get('workers'), list):
        rows = {w['station_code']: {k: w.get(k, d.get(k)) for k in PHYSICAL_KEYS}
                for w in d['workers']}
        if len(rows) != len(d['workers']):
            raise ValueError('Duplicate source station')
    elif isinstance(d.get('stations'), dict) and isinstance(d.get('models'), dict):
        rows = {code: {**{k: row.get(k) for k in PHYSICAL_KEYS},
                       **{k: d['models'].get(k) for k in ('vic_exe', 'rout_exe')}}
                for code, row in d['stations'].items()}
    else:
        raise ValueError('Use an existing CLI team config or traditional staged_manifest.json, not optimizer results')
    if not rows or any(not isinstance(code, str) or not code or code in ('.', '..')
                       or any(c.isspace() or c in '/\\' for c in code) for code in rows):
        raise ValueError('Physical binding needs safe nonempty station identities')
    if expected_stations is not None and set(rows) != set(expected_stations):
        raise ValueError('Physical binding stations differ from the configured worker stations')
    for code, row in rows.items():
        for key in PHYSICAL_KEYS:
            if not row.get(key) or not Path(row[key]).is_absolute():
                raise ValueError(f'{code}: missing absolute physical path {key}')
            p = Path(row[key])
            if not (p.is_dir() if key == 'rout_template_dir' else p.is_file()):
                raise ValueError(f'{code}: missing {key}: {p}')
            # Keep a valid alias rather than resolving it into a path with spaces.
            row[key] = str(p)
    return rows


class InputPlan:
    def __init__(self, work):
        self.work = work
        self.files, self.links, self.externals, self.forcing = {}, {}, {}, {}
        self._aliases = {}

    def alias(self, target, prefix=False):
        target = Path(target)
        base = target.parent if prefix else target
        if not base.exists():
            raise ValueError('Shared physical input does not exist: ' + str(base))
        key = str(base)
        if key not in self._aliases:
            rel = 'refs/input_' + str(len(self._aliases)).zfill(4)
            self._aliases[key] = rel
            self.links[rel] = str(base)
        result = self.work / self._aliases[key]
        if prefix:
            if any(c.isspace() for c in target.name):
                raise ValueError('Whitespace in forcing prefix is not supported')
            result /= target.name
        elif base.is_file():
            self.externals[str(base)] = sha256(base)
        return str(result)

    def global_text(self, source, code, soil):
        out, found, forcing_prefixes, decimals, model_options = [], set(), [], 4, {}
        for raw in Path(source).read_text().splitlines():
            tokens = shlex.split(raw, comments=True)
            if not tokens:
                out.append(raw); continue
            key, args = tokens[0].upper(), tokens[1:]
            if key in ('BASEFLOW', 'NLAYER'):
                model_options[key] = args
            if key == 'GRID_DECIMAL':
                decimals = int(args[0])
            if key in ('SOIL', 'RESULT_DIR'):
                val = self.work / ('inputs/' + code + '/soil.txt' if key == 'SOIL'
                                   else 'template_unused/' + code)
                out.append(key + ' ' + str(val)); found.add(key); continue
            if key in ('FORCING1', 'FORCING2', 'VEGPARAM', 'VEGLIB', 'INIT_STATE', 'SNOW_BAND'):
                if not args:
                    raise ValueError('Malformed global option: ' + key)
                if args[-1].upper() in ('FALSE', 'NONE') or (key == 'SNOW_BAND' and len(args) == 1):
                    out.append(raw); continue
                target = Path(args[-1])
                if not target.is_absolute(): target = Path(source).parent / target
                is_prefix = key.startswith('FORCING')
                args[-1] = self.alias(target, prefix=is_prefix)
                if is_prefix: forcing_prefixes.append(target)
                found.add(key); out.append(' '.join([key, *args])); continue
            if any('/' in arg or '\\' in arg for arg in args):
                raise ValueError('Unreviewed global path option: ' + key)
            out.append(raw)
        if not {'SOIL', 'RESULT_DIR', 'FORCING1', 'VEGPARAM', 'VEGLIB'} <= found:
            raise ValueError('Global template lacks required physical input declarations')
        if model_options != {'BASEFLOW': ['ARNO'], 'NLAYER': ['3']}:
            raise ValueError('Matched mechanism reference requires BASEFLOW ARNO and NLAYER 3: ' + code)
        cells = []
        for line in Path(soil).read_text().splitlines():
            row = line.split()
            if row and not row[0].startswith('#') and int(row[0]) == 1:
                cells.append((float(row[2]), float(row[3])))
        if not cells or len(cells) != len(set(cells)):
            raise ValueError('Empty or duplicate active soil cells: ' + code)
        for prefix in forcing_prefixes:
            for lat, lon in cells:
                path = Path(str(prefix) + f'{lat:.{decimals}f}_{lon:.{decimals}f}')
                if not path.is_file() or path.stat().st_size <= 0:
                    raise ValueError('Missing or empty forcing file: ' + str(path))
                stat = path.stat()
                self.forcing[str(path)] = [stat.st_size, stat.st_mtime_ns]
        return ('\n'.join(out) + '\n').encode()

    def station(self, code, row, flux_prefix="fluxes_"):
        base = 'inputs/' + code + '/'
        self.files[base + 'soil.txt'] = Path(row['soil_base']).read_bytes()
        self.files[base + 'global.txt'] = self.global_text(row['global_base'], code, row['soil_base'])
        routing = Path(row['rout_template_dir'])
        members = [p for p in routing.rglob('*') if p.is_file() and not p.name.startswith('.')]
        if not members or not any(p.name.endswith('_direc.txt') for p in members):
            raise ValueError('Incomplete routing template: ' + code)
        for p in members:
            if p.stat().st_size > 1024 * 1024:
                raise ValueError('Routing binding includes large non-template data: ' + str(p))
            data = p.read_bytes()
            if p.name.endswith('rout_global.txt'):
                lines = []
                for line in data.decode().splitlines():
                    val = line.strip()
                    if val.startswith('/'):
                        if 'vic_in' in val: line = './vic_in/' + flux_prefix
                        elif 'rout_out' in val: line = './rout_out/'
                        else:
                            target = Path(val)
                            if not target.is_file() or target.parent.resolve() != routing.resolve():
                                raise ValueError('External routing reference needs a prepared template: ' + val)
                            line = './' + target.name
                    lines.append(line)
                data = ('\n'.join(lines) + '\n').encode()
            self.files[base + 'routing/' + str(p.relative_to(routing))] = data
        return {'soil_base': str(self.work / (base + 'soil.txt')),
                'global_base': str(self.work / (base + 'global.txt')),
                'rout_template_dir': str(self.work / (base + 'routing')),
                **{k: self.alias(row[k]) for k in ('obs_file', 'vic_exe', 'rout_exe')}}

