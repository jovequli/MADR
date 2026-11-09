"""Validated fresh/reuse/replay exploration. No LLM calls; cache never changes evidence cost."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import random

from .execution_protocol import candidate_score, is_finite, require_effective_params, SOIL_KEYS
from .input_fingerprint import build_input_identity, canonical_bytes, file_identity, fingerprint
from .initial_explorer import run_initial_exploration, _compact_params

SCHEMA = "madr-exploration-bundle-v1"


class ExplorationProtocolError(ValueError):
    pass


def _json_value(value):
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if hasattr(value, "item"):
        return _json_value(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_new(path, value):
    data = canonical_bytes(_json_value(value)) + b"\n"
    # Exclusive final publication only after complete in-memory validation.
    with Path(path).open("xb") as stream:
        stream.write(data)


def _load_json(path):
    def reject_pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ExplorationProtocolError("Duplicate JSON key: " + key)
            output[key] = value
        return output
    return json.loads(Path(path).read_text(), object_pairs_hook=reject_pairs)


def _counts(settings):
    result = []
    for key, default in (("n_near", 10), ("n_random", 40)):
        value = settings.get(key, default)
        if type(value) is not int or value < 0:
            raise ExplorationProtocolError("Exploration count must be a nonnegative integer: " + key)
        result.append(value)
    if sum(result) < 1:
        raise ExplorationProtocolError("Enabled exploration requires at least one candidate")
    return tuple(result)


def _source_path(settings):
    source, alias = settings.get("source"), settings.get("seed_from_run")
    if source and alias and Path(source).expanduser().resolve() != Path(alias).expanduser().resolve():
        raise ExplorationProtocolError("source and seed_from_run refer to different runs")
    selected = source or alias
    if not selected:
        raise ExplorationProtocolError("reuse/replay requires source (seed_from_run is an alias)")
    path = Path(selected).expanduser().resolve()
    if not path.exists():
        raise ExplorationProtocolError("Exploration source does not exist: " + str(path))
    return path


def _bundle_path(source):
    return source / "exploration_bundle.json" if source.is_dir() else source


def _record_result(sid, records, active_keys):
    ordered = sorted(records, key=lambda r: r["nse"], reverse=True)
    if not ordered or not is_finite(ordered[0]["nse"]):
        raise ExplorationProtocolError("No valid exploration candidate: " + sid)
    best = ordered[0]
    top5 = [r["nse"] for r in ordered[:5]]
    summary = (f"{sid}: best_NSE={best['nse']:.4f} ({best['label']}) | "
               f"top5=[{', '.join(f'{n:.3f}' for n in top5)}] | "
               f"params={_compact_params(best['params'], active_keys)}")
    return {"best_params": copy.deepcopy(best["params"]), "best_nse": best["nse"],
            "best_metrics": copy.deepcopy(best["metrics"]), "all_results": ordered,
            "n_samples": len(ordered), "summary_text": summary}


def _validate_results(raw_results, station_order, count, active_keys):
    if not isinstance(raw_results, dict) or set(raw_results) != set(station_order):
        raise ExplorationProtocolError("Exploration must contain exactly the configured stations")
    validated = {}
    for sid in station_order:
        result = raw_results[sid]
        records = result.get("all_results", [])
        if len(records) != count or result.get("n_samples") != count:
            raise ExplorationProtocolError("Incomplete exploration sample count: " + sid)
        ids = [r.get("sample_id") for r in records]
        if any(type(i) is not int for i in ids) or set(ids) != set(range(count)) or len(set(ids)) != count:
            raise ExplorationProtocolError("Missing or duplicate exploration sample IDs: " + sid)
        normalized_records = []
        for record in sorted(records, key=lambda r: r["sample_id"]):
            require_effective_params(record["params"])
            if type(record.get("ok")) is not bool or not isinstance(record.get("metrics"), dict):
                raise ExplorationProtocolError("Invalid exploration status or metrics: " + sid)
            score = candidate_score(record["ok"], record["metrics"])
            if _json_value(record.get("nse")) != _json_value(score):
                raise ExplorationProtocolError("Stored exploration score disagrees with candidate metrics: " + sid)
            if not isinstance(record.get("label"), str):
                raise ExplorationProtocolError("Missing exploration sample label: " + sid)
            normalized_records.append({**record, "nse": score})
        recomputed = _record_result(sid, normalized_records, active_keys)
        for key in ("best_params", "best_nse", "best_metrics", "summary_text"):
            if _json_value(result.get(key)) != _json_value(recomputed[key]):
                raise ExplorationProtocolError("Exploration summary/best does not match samples: " + sid + "/" + key)
        validated[sid] = recomputed
    return validated


def _read_bundle(path):
    bundle = _load_json(path)
    if bundle.get("schema") != SCHEMA or bundle.get("complete") is not True:
        raise ExplorationProtocolError("Source is not a complete verified bundle; legacy results require replay")
    identity = bundle.get("identity")
    if not isinstance(identity, dict) or fingerprint(identity) != bundle.get("protocol_sha256"):
        raise ExplorationProtocolError("Exploration protocol fingerprint mismatch")
    expected = fingerprint({k: bundle[k] for k in ("identity", "results", "source_provenance")})
    if expected != bundle.get("payload_sha256"):
        raise ExplorationProtocolError("Exploration samples/summary content fingerprint mismatch")
    count = identity["design"]["n_near"] + identity["design"]["n_random"]
    results = _validate_results(bundle["results"], identity["station_order"], count,
                                identity["design"]["active_keys"])
    return bundle, results


def _set_memory(orchestrator, results):
    memory = orchestrator.leader.memory.latest_best_by_subbasin
    for sid, result in results.items():
        previous = memory.get(sid, {}).get("metrics", {}).get("NSE")
        if not is_finite(previous) or result["best_nse"] > previous:
            memory[sid] = {"round_id": 0, "params": copy.deepcopy(result["best_params"]),
                           "metrics": copy.deepcopy(result["best_metrics"])}


def _read_replay(source, station_order, count):
    candidate_bundle = _bundle_path(source)
    if candidate_bundle.is_file():
        bundle, results = _read_bundle(candidate_bundle)
        if set(results) != set(station_order):
            raise ExplorationProtocolError("Replay source stations differ")
        records = {sid: result["all_results"] for sid, result in results.items()}
        provenance = {"kind": "verified_bundle_parameter_replay", "path": str(candidate_bundle),
                      "source_sha256": file_identity(candidate_bundle)["sha256"],
                      "source_protocol_sha256": bundle["protocol_sha256"]}
    else:
        directory = source / "exploration" if source.is_dir() and (source / "exploration").is_dir() else source
        if not directory.is_dir():
            raise ExplorationProtocolError("Replay source must contain exploration/{subbasin_id}.json")
        files = {p.stem: p for p in directory.glob("*.json") if not p.name.startswith(".")}
        if set(files) != set(station_order):
            raise ExplorationProtocolError("Historical replay must contain exactly the configured stations")
        records, identities = {}, {}
        for sid in station_order:
            data = _load_json(files[sid])
            if data.get("subbasin_id", sid) != sid:
                raise ExplorationProtocolError("Replay source basin label mismatch: " + sid)
            records[sid] = data.get("samples", [])
            identities[sid] = file_identity(files[sid])
        provenance = {"kind": "historical_parameters_only", "path": str(directory), "files": identities,
                      "source_protocol": "unverified; previous scores and summary are never inherited"}
    samples = {}
    for sid in station_order:
        if len(records[sid]) != count:
            raise ExplorationProtocolError("Replay sample count differs from requested design: " + sid)
        ids = [r.get("sample_id") for r in records[sid]]
        if any(type(i) is not int for i in ids) or set(ids) != set(range(count)) or len(set(ids)) != count:
            raise ExplorationProtocolError("Missing or duplicate replay sample IDs: " + sid)
        samples[sid] = []
        for record in sorted(records[sid], key=lambda r: r["sample_id"]):
            try:
                require_effective_params(record["params"])
            except (ValueError, KeyError, TypeError) as error:
                raise ExplorationProtocolError(
                    f"Replay rejects invalid historical effective parameters at {sid}/sample_{record.get('sample_id')}; "
                    "no silent projection is allowed. Generate a fresh design instead. " + str(error)) from error
            samples[sid].append(copy.deepcopy(record["params"]))
    provenance["parameter_set_sha256"] = fingerprint(samples)
    return samples, provenance


def _evaluate_replay(orchestrator, samples, n_near, active_keys):
    results = {}
    for level in orchestrator.topo_levels:
        for sid in level:
            routing = orchestrator._build_upstream_routing_info(sid)
            soil = orchestrator._build_upstream_soil_info(sid)
            records = []
            for i, params in enumerate(samples[sid]):
                ok, metrics = orchestrator.vic_executor.run(
                    subbasin_id=sid, run_id=i+1, candidate_id=i+1, params=params,
                    phase_tag=f"init_explore_replay_{i+1:03d}",
                    upstream_routing_info=routing, upstream_soil_info=soil)
                records.append({"sample_id": i, "params": params, "metrics": metrics if ok else {},
                                "nse": candidate_score(ok, metrics), "ok": bool(ok),
                                "label": "replay", "source_sample_id": i})
            results[sid] = _record_result(sid, records, active_keys)
            _set_memory(orchestrator, {sid: results[sid]})
    return results


class _CountingExecutor:
    def __init__(self, executor):
        self.executor, self.requests = executor, 0
    def __getattr__(self, name):
        return getattr(self.executor, name)
    def run(self, *args, **kwargs):
        self.requests += 1
        return self.executor.run(*args, **kwargs)


def _accounting(executor):
    if not hasattr(executor, "get_accounting"):
        return None
    return copy.deepcopy(executor.get_accounting())


def run_exploration_protocol(orchestrator, config, output_root):
    """Return the legacy exploration result mapping, with an independently verified bundle.

    ``initial_exploration.mode`` is fresh/reuse/replay; ``source`` and
    ``seed_from_run`` are equivalent source selectors. Disabled never opens source.
    Reusing a replay-generated bundle additionally requires an explicit
    ``replay_parameter_set_sha256`` to avoid calling historical parameters a fresh design.
    """
    settings = config.get("initial_exploration", {})
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    if any((output_root / name).exists() for name in ("exploration_bundle.json", "exploration_receipt.json")):
        raise ExplorationProtocolError("Exploration output already exists; use a new run directory instead of evaluating twice")
    if not settings.get("enabled", False):
        _write_new(output_root / "exploration_receipt.json", {
            "mode": "disabled", "evaluation_requests": 0, "physical_execution_requests": 0,
            "actual_vic_calls": 0, "actual_routing_calls": 0, "inherited_evaluations": 0,
            "cache_hits": 0, "source_read": False})
        return {}
    mode = settings.get("mode", "fresh")
    if mode not in ("fresh", "reuse", "replay"):
        raise ExplorationProtocolError("Exploration mode must be fresh, reuse or replay")
    n_near, n_random = _counts(settings)
    seed = settings.get("seed", config.get("seed", config.get("random_seed")))
    if type(seed) is not int or seed < 0:
        raise ExplorationProtocolError("An explicit nonnegative exploration seed is required")
    active = list(SOIL_KEYS)
    design = {"n_near": n_near, "n_random": n_random, "seed": seed,
              "active_keys": active, "near_spread": .15,
              "sampling": "historical_near_gaussian_plus_stratified_uniform_up_to_50",
              "centers_updated": False}
    identity, local_paths = build_input_identity(orchestrator, config, design)
    station_order = identity["station_order"]
    source = None
    source_provenance = {"kind": "fresh_generated", "seed": seed}
    results = None
    if mode == "reuse":
        source = _source_path(settings)
        path = _bundle_path(source)
        if not path.is_file():
            raise ExplorationProtocolError("No verified bundle; legacy exploration must use mode=replay")
        bundle, results = _read_bundle(path)
        if bundle["identity"]["design"].get("sampling") == "replayed_source_parameters":
            required = bundle["identity"]["design"].get("parameter_set_sha256")
            if settings.get("replay_parameter_set_sha256") != required:
                raise ExplorationProtocolError("Reusing a replay design requires explicit replay_parameter_set_sha256")
            identity["design"].update(sampling="replayed_source_parameters", parameter_set_sha256=required)
        if fingerprint(identity) != bundle["protocol_sha256"]:
            raise ExplorationProtocolError("Exploration protocol/input/seed differs; silent score reuse forbidden. Use replay to reevaluate legal source parameters.")
        source_provenance = {"kind": "verified_reuse", "path": str(path),
                             "source_sha256": file_identity(path)["sha256"],
                             "source_protocol_sha256": bundle["protocol_sha256"],
                             "source_payload_sha256": bundle["payload_sha256"]}
        _set_memory(orchestrator, results)
    elif mode == "replay":
        source = _source_path(settings)
        samples, source_provenance = _read_replay(source, station_order, n_near+n_random)
        identity["design"].update(sampling="replayed_source_parameters",
                                  parameter_set_sha256=source_provenance["parameter_set_sha256"])
    original_executor = orchestrator.vic_executor
    before = _accounting(original_executor)
    counter = _CountingExecutor(original_executor)
    if mode != "reuse":
        state = random.getstate()
        orchestrator.vic_executor = counter
        try:
            random.seed(seed)
            results = (run_initial_exploration(orchestrator, n_near=n_near, n_random=n_random,
                                              vic_only_keys=active) if mode == "fresh"
                       else _evaluate_replay(orchestrator, samples, n_near, active))
        finally:
            orchestrator.vic_executor = original_executor
            random.setstate(state)
    after = _accounting(original_executor)
    results = _validate_results(results, station_order, n_near+n_random, active)
    bundle = {"schema": SCHEMA, "complete": True, "identity": identity,
              "protocol_sha256": fingerprint(identity), "results": _json_value(results),
              "source_provenance": source_provenance}
    bundle["payload_sha256"] = fingerprint({k: bundle[k] for k in ("identity", "results", "source_provenance")})
    count = len(station_order) * (n_near+n_random)
    real = identity["execution_mode"] == "real"
    receipt = {"mode": mode, "execution_mode": identity["execution_mode"], "source": source_provenance,
               "protocol_sha256": bundle["protocol_sha256"], "evaluation_requests": counter.requests,
               "physical_execution_requests": counter.requests if real else 0,
               "inherited_evaluations": count if mode == "reuse" else 0,
               "cache_hits": count if mode == "reuse" else 0,
               "information_budget_evaluations": count,
               "actual_vic_calls": 0 if mode == "reuse" or not real else None,
               "actual_routing_calls": 0 if mode == "reuse" or not real else None,
               "local_input_references": local_paths,
               "counter_note": "Requests are not actual process calls; absent executor launch counters remain null."}
    if before is not None and after is not None:
        for key, dest in (("actual_vic_launches", "actual_vic_calls"),
                          ("actual_routing_launches", "actual_routing_calls")):
            if type(before.get(key)) is int and type(after.get(key)) is int:
                receipt[dest] = after[key] - before[key]
        receipt["executor_accounting_before"] = before
        receipt["executor_accounting_after"] = after
    _write_new(output_root / "exploration_bundle.json", bundle)
    _write_new(output_root / "exploration_receipt.json", receipt)
    return results
