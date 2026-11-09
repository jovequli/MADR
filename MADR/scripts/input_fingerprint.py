"""Content identity for exploration sharing; reads physical inputs, never launches models."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import platform
import shlex

from .execution_protocol import DECIMALS, EFFECTIVE_BOUNDS, require_effective_params, validate_periods


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_identity(path):
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError("Required physical input file does not exist: " + str(path))
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return {"sha256": h.hexdigest(), "bytes": path.stat().st_size}


def _resolve(token, parent):
    path = Path(token).expanduser()
    return path if path.is_absolute() else parent / path


def _tree_identity(directory):
    root = Path(directory)
    if not root.is_dir():
        raise ValueError("Required input directory does not exist: " + str(root))
    files = {str(p.relative_to(root)): file_identity(p) for p in sorted(root.rglob("*"))
             if p.is_file() and not p.name.startswith(".")}
    if not files:
        raise ValueError("Empty physical input directory: " + str(root))
    return files


def _soil_cells(path):
    cells = []
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) < 25:
            raise ValueError("Malformed soil row in input identity")
        if int(fields[0]) == 1:
            cells.append((float(fields[2]), float(fields[3])))
    if not cells or len(cells) != len(set(cells)):
        raise ValueError("Empty or duplicate active soil cells")
    return sorted(cells)


def _global_identity(path, soil, config, references):
    """Canonicalize recognized path tokens, hash their physical contents separately.

    Relative input references are interpreted relative to the global template.
    Deployment must give the executor equivalent resolved references.
    """
    path = Path(path)
    directives = []
    for line in path.read_text().splitlines():
        tokens = shlex.split(line, comments=True)
        if tokens:
            directives.append(tokens)
    values = {}
    for row in directives:
        values.setdefault(row[0].upper(), []).append(row[1:])
    decimals = int(values.get("GRID_DECIMAL", [["4"]])[-1][0])
    if not 0 <= decimals <= 12:
        raise ValueError("Unsupported forcing coordinate precision")
    canonical = []
    dependencies = {}
    simulation = config["simulation_period"]
    overrides = {"STARTYEAR": str(simulation["start_year"]), "STARTMONTH": "1", "STARTDAY": "1",
                 "ENDYEAR": str(simulation["end_year"]), "ENDMONTH": "12", "ENDDAY": "31"}
    for row in directives:
        key, args = row[0].upper(), row[1:]
        if key in overrides:
            canonical.append([key, overrides[key]])
        elif key == "RESULT_DIR":
            canonical.append([key, "<runtime-output>"])
        elif key == "SOIL":
            canonical.append([key, "<configured-soil>"])
        elif key in ("FORCING1", "FORCING2"):
            if len(args) != 1:
                raise ValueError("Unsupported forcing declaration")
            if args[0].upper() in ("FALSE", "NONE"):
                canonical.append([key, args[0].upper()])
                continue
            prefix = _resolve(args[0], path.parent)
            files = {}
            for lat, lon in _soil_cells(soil):
                coordinate = f"{lat:.{decimals}f}_{lon:.{decimals}f}"
                target = Path(str(prefix) + coordinate)
                files[coordinate] = file_identity(target)
            dependencies[key] = files
            references[key] = str(prefix)
            canonical.append([key, "<forcing-prefix>"])
        elif key in ("VEGPARAM", "VEGLIB", "INIT_STATE", "SNOW_BAND"):
            if key == "SNOW_BAND" and len(args) == 1:
                canonical.append([key, *args])
                continue
            if not args:
                raise ValueError("Missing physical input reference: " + key)
            token = args[-1]
            if token.upper() in ("FALSE", "NONE"):
                canonical.append([key, *args])
                continue
            target = _resolve(token, path.parent)
            dependencies[key] = file_identity(target)
            references[key] = str(target)
            canonical.append([key, *args[:-1], "<physical-file>"])
        else:
            normalized = [key]
            for index, token in enumerate(args):
                # Unknown file directives are conservatively identified by contents.
                if "/" in token or "\\" in token:
                    target = _resolve(token, path.parent)
                    label = key + ":" + str(index)
                    dependencies[label] = _tree_identity(target) if target.is_dir() else file_identity(target)
                    references[label] = str(target)
                    normalized.append("<physical-input>")
                else:
                    normalized.append(token)
            canonical.append(normalized)
    if "FORCING1" not in dependencies:
        raise ValueError("A real exploration requires a verifiable FORCING1 prefix")
    for key in ("VEGPARAM", "VEGLIB"):
        if key not in dependencies:
            raise ValueError("Missing required vegetation input: " + key)
    return {"directives": canonical, "dependencies": dependencies}


def _routing_identity(directory, references):
    root = Path(directory)
    files = _tree_identity(root)
    for rel in list(files):
        path = root / rel
        if not path.name.endswith("rout_global.txt"):
            continue
        lines = []
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "vic_in" in stripped or "rout_out" in stripped:
                lines.append("<runtime-flux-or-output>")
            elif stripped.startswith("/"):
                target = Path(stripped)
                # Absolute references are identified by contents, not machine prefix.
                ident = file_identity(target)
                lines.append("<input:" + ident["sha256"] + ">")
                references["routing:" + rel + ":" + str(len(lines))] = str(target)
            else:
                lines.append(stripped)
        files[rel] = {"canonical_sha256": fingerprint(lines)}
    return files


def _module_ast_identity(path, names=None):
    tree = ast.parse(Path(path).read_text())
    if names is not None:
        tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name in names]
        found = {node.name for node in tree.body}
        if found != set(names):
            raise ValueError("Missing protocol functions: " + repr(set(names) - found))
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def code_identity():
    root = Path(__file__).resolve().parent
    # No LLM/provider/role code belongs to numerical exploration identity.
    selected = {
        "execution_protocol.py": ["_finite", "normalize", "is_finite", "candidate_score", "score_period",
                                  "require_effective_values", "require_effective_soil", "require_effective_params"],
        "vic_runner.py": None,
        "vic_executor.py": None,
        "initial_explorer.py": None,
        "param_validator.py": ["normalize_params"],
        "graph_nodes.py": ["_collect_all_upstream", "_build_upstream_routing_info", "_build_upstream_soil_info"],
        "routing_utils.py": None,
        "exploration_reuse.py": None,
        "input_fingerprint.py": None,
    }
    return {filename: _module_ast_identity(root / filename, names) for filename, names in selected.items()}


def build_input_identity(orchestrator, config, design):
    """Return (path-free protocol identity, path-bearing local provenance)."""
    validate_periods(config)
    station_order = [sid for level in orchestrator.topo_levels for sid in level]
    cfg_workers = {w["subbasin_id"]: w for w in config["workers"]}
    if len(station_order) != len(set(station_order)) or set(station_order) != set(cfg_workers):
        raise ValueError("Exploration requires each configured basin exactly once")
    centers = {sid: dict(orchestrator.centers[sid]) for sid in station_order}
    for params in centers.values():
        require_effective_params(params)
    if {k: tuple(v) for k, v in orchestrator.param_ranges.items()} != EFFECTIVE_BOUNDS:
        raise ValueError("Exploration bounds differ from frozen effective protocol")
    mode = getattr(orchestrator.vic_executor, "mode", config.get("model_binding", {}).get("mode", "real"))
    if mode not in ("real", "simulate"):
        raise ValueError("Unknown exploration execution mode")
    identity = {"schema": "madr-exploration-inputs-v1", "execution_mode": mode,
                "simulation_period": config["simulation_period"], "warmup_period": config["warmup_period"],
                "evaluation_period": config["evaluation_period"], "effective_bounds": EFFECTIVE_BOUNDS,
                "decimals": DECIMALS, "depth_constraint": "soil_d3 > soil_d2 after quantization",
                "station_order": station_order, "topo_levels": orchestrator.topo_levels,
                "upstream_map": getattr(orchestrator, "upstream_map", {}),
                "full_upstream_map": getattr(orchestrator, "full_upstream_map", {}),
                "centers": centers, "design": design, "code": code_identity(),
                "python_version": platform.python_version(), "stations": {}}
    import numpy
    identity["numpy_version"] = numpy.__version__
    provenance = {"stations": {}}
    if mode == "simulate":
        identity["simulation_notice"] = "synthetic executor; cannot reuse as real-model exploration"
        return identity, provenance
    runtime_configs = getattr(orchestrator.vic_executor, "_vic_configs", {})
    for sid in station_order:
        worker = cfg_workers[sid]
        actual = runtime_configs.get(sid)
        def get(key):
            return getattr(actual, key) if actual is not None and hasattr(actual, key) else worker.get(key, config.get(key, ""))
        refs = {key: str(get(key)) for key in ("soil_base", "global_base", "obs_file", "rout_template_dir", "vic_exe", "rout_exe")}
        station = {key: file_identity(refs[key]) for key in ("soil_base", "obs_file", "vic_exe", "rout_exe")}
        station["global"] = _global_identity(refs["global_base"], refs["soil_base"], config, refs)
        station["routing_template"] = _routing_identity(refs["rout_template_dir"], refs)
        if actual is not None:
            for attribute, expected in (("start_year", config["simulation_period"]["start_year"]),
                                        ("end_year", config["simulation_period"]["end_year"]),
                                        ("eval_start_year", config["evaluation_period"]["start_year"]),
                                        ("eval_end_year", config["evaluation_period"]["end_year"])):
                if getattr(actual, attribute, expected) != expected:
                    raise ValueError("Executor period differs from exploration protocol: " + sid)
        if get("vic_flux_source"):
            # Initial exploration tunes soil: inherited flux would defeat that design.
            raise ValueError("Initial exploration cannot run against fixed routing-only flux")
        identity["stations"][sid] = station
        provenance["stations"][sid] = refs
    fixed = getattr(orchestrator, "fixed_upstream_routing", {})
    if isinstance(fixed, list):
        fixed = {row["subbasin_id"]: row for row in fixed}
    if fixed:
        identity["fixed_upstream_routing"] = {}
        for sid, params in sorted(fixed.items()):
            identity["fixed_upstream_routing"][sid] = {
                "velocity": params["velocity"], "diffusivity": params["diffusivity"],
                "routing_template": _routing_identity(params["rout_template_dir"], {})}
    return identity, provenance


def build_input_fingerprint(config, executor):
    """Public startup/resume input identity, independent of whether exploration is enabled.

    Returns ``{sha256, identity, provenance}``. Only ``identity`` enters the digest;
    ``provenance`` retains local paths for audit. Role and LLM settings are omitted.
    """
    from types import SimpleNamespace
    workers = config["workers"]
    ids = [w["subbasin_id"] for w in workers]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate configured basin")
    upstream = {sid: [] for sid in ids}
    for worker in workers:
        for downstream in worker.get("neighbors_downstream", []):
            if downstream not in upstream:
                raise ValueError("Unknown downstream basin")
            upstream[downstream].append(worker["subbasin_id"])
    remaining = set(ids)
    levels = []
    while remaining:
        ready = [sid for sid in ids if sid in remaining and not (set(upstream[sid]) & remaining)]
        if not ready:
            raise ValueError("Cyclic basin topology")
        levels.append(ready)
        remaining.difference_update(ready)
    center = config["initial_param_center"]
    fixed = config.get("fixed_upstream_routing", {})
    if isinstance(fixed, list):
        fixed = {row["subbasin_id"]: row for row in fixed}
    adapter = SimpleNamespace(vic_executor=executor, topo_levels=levels,
        centers={sid: dict(center) for sid in ids}, param_ranges=config["param_ranges"],
        upstream_map=upstream, full_upstream_map=config.get("full_upstream_map", {}),
        fixed_upstream_routing=fixed)
    identity, provenance = build_input_identity(adapter, config, {"scope": "startup_physical_protocol"})
    return {"sha256": fingerprint(identity), "identity": identity, "provenance": provenance}
