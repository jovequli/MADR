"""Shared parameter normalization, evaluation periods and score validation.

No optimizer, model call or file mutation happens on import."""
import datetime as dt
import json
import math
from dataclasses import dataclass, asdict

KEYS = ("binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3", "velocity", "diffusivity")
SOIL_KEYS = KEYS[:6]
EFFECTIVE_BOUNDS = dict(zip(KEYS, ((.001, .4), (.001, 1), (1, 30), (.5, 1), (.1, 2), (.1, 2), (.5, 3), (200, 4000))))
DECIMALS = {**dict.fromkeys(SOIL_KEYS, 3), "velocity": 2, "diffusivity": 0}

STATIC_INPUT_PROTOCOL = "minimal_static_sources_v1"

@dataclass(frozen=True)
class StaticInputProtocol:
    """One explicit source/content choice with defaults for direct constructors."""
    protocol: str = STATIC_INPUT_PROTOCOL
    storage: str = "markdown"
    worker_content: str = "minimal"

    def __post_init__(self):
        if self.protocol != STATIC_INPUT_PROTOCOL:
            raise ValueError("Unsupported static input protocol")
        if self.storage != "markdown":
            raise ValueError("static_input_protocol.storage must be markdown")
        if self.worker_content != "minimal":
            raise ValueError("Unsupported static_input_protocol.worker_content")


def validate_static_input_protocol(value=None):
    """Validate an explicit protocol dictionary or use constructor defaults.

    Deployment tools separately require and seal all three fields. No unknown
    keys or partial supplied dictionaries silently choose another source.
    """
    if value is None:
        return asdict(StaticInputProtocol())
    if isinstance(value, StaticInputProtocol):
        return asdict(value)
    keys = {"protocol", "storage", "worker_content"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("static_input_protocol requires exactly protocol, storage, worker_content")
    if any(not isinstance(value[k], str) for k in keys):
        raise ValueError("static_input_protocol values must be strings")
    return asdict(StaticInputProtocol(**value))

def validate_worker_evidence_mode(value="existing"):
    """Validate the Worker evidence mode; the default is existing."""
    if value != "existing":
        raise ValueError("worker_evidence_mode must be existing")
    return value

def _finite(value):
    if isinstance(value, bool):
        raise ValueError("Boolean parameter is not a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Nonfinite parameter")
    return value

def normalize(raw, bounds=None):
    """Return eight effective values, preserving legacy rounding except unsafe edges."""
    if set(raw) != set(KEYS):
        raise ValueError("Exactly the eight named parameters are required")
    bounds = EFFECTIVE_BOUNDS if bounds is None else bounds
    if set(bounds) != set(KEYS):
        raise ValueError("Bounds must cover exactly eight parameters")
    grid = {}
    p = {}
    for k in KEYS:
        lo, hi = map(_finite, bounds[k])
        lo = max(lo, EFFECTIVE_BOUNDS[k][0])
        hi = min(hi, EFFECTIVE_BOUNDS[k][1])
        if lo > hi:
            raise ValueError("Empty bounds: " + k)
        scale = 10 ** DECIMALS[k]
        qlo = math.ceil(lo * scale - 1e-9) / scale
        qhi = math.floor(hi * scale + 1e-9) / scale
        if qlo > qhi:
            raise ValueError("No representable value: " + k)
        grid[k] = (qlo, qhi)
        p[k] = min(hi, max(lo, _finite(raw[k])))

    def repair_depth():
        if p["soil_d3"] > p["soil_d2"]:
            return
        p["soil_d3"] = min(grid["soil_d3"][1], max(grid["soil_d3"][0], p["soil_d2"] + .05))
        if p["soil_d3"] <= p["soil_d2"]:
            p["soil_d2"] = max(grid["soil_d2"][0], min(grid["soil_d2"][1], p["soil_d3"] - .05))
        if p["soil_d3"] <= p["soil_d2"]:
            raise ValueError("No feasible quantized depth pair")

    repair_depth()
    for k in KEYS:
        p[k] = min(grid[k][1], max(grid[k][0], round(p[k], DECIMALS[k])))
    # Restore strict depth ordering within bounds after precision rounding.
    repair_depth()
    p = {k: round(p[k], DECIMALS[k]) for k in KEYS}
    if not p["soil_d3"] > p["soil_d2"]:
        raise ValueError("No feasible quantized depth pair after rounding")
    for k in KEYS:
        if not grid[k][0] <= p[k] <= grid[k][1]:
            raise ValueError("Post-normalization bound violation: " + k)
    return p

def is_finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def candidate_score(ok, metrics):
    value = metrics.get("NSE")
    return float(value) if ok and is_finite(value) else float("-inf")

def require_effective_values(params, keys):
    if not set(keys) <= set(params):
        raise ValueError("Missing effective parameters")
    for k in keys:
        value = params[k]
        if not is_finite(value):
            raise ValueError("Effective parameter must be a finite number: " + k)
        lo, hi = EFFECTIVE_BOUNDS[k]
        if not lo <= value <= hi or round(value, DECIMALS[k]) != value:
            raise ValueError("Execution requires effective quantized values: " + k)

def require_effective_soil(params):
    require_effective_values(params, SOIL_KEYS)
    if params["soil_d3"] <= params["soil_d2"]:
        raise ValueError("Soil writer requires d3 > d2")

def require_effective_params(params):
    if set(params) != set(KEYS):
        raise ValueError("Execution requires exactly eight effective parameters")
    require_effective_values(params, KEYS)
    require_effective_soil(params)

def require_execution_inputs(params, routing_params=None, upstream_routing_info=None, upstream_soil_info=None):
    # This gate does not silently repair values: saved effective parameters must
    # already be exactly what the model will receive, including routing-only runs.
    require_effective_params(params)
    if routing_params is not None:
        if set(routing_params) != set(KEYS[6:]):
            raise ValueError("Exactly two effective routing parameters are required")
        require_effective_values(routing_params, KEYS[6:])
        if any(routing_params[k] != params[k] for k in KEYS[6:]):
            raise ValueError("Routing override differs from recorded effective parameters")
    for info in upstream_soil_info or []:
        require_effective_soil(info.params)
    for info in upstream_routing_info or []:
        require_effective_values({"velocity": info.velocity, "diffusivity": info.diffusivity}, KEYS[6:])

def validate_periods(config):
    periods = {}
    for key in ("simulation_period", "warmup_period", "evaluation_period"):
        if key not in config:
            raise ValueError("Explicit " + key + " is required")
        p = config[key]
        a, b = p.get("start_year"), p.get("end_year")
        if type(a) is not int or type(b) is not int or a > b:
            raise ValueError("Invalid " + key)
        periods[key] = (a, b)
    sim, warm, evaluation = (periods[k] for k in ("simulation_period", "warmup_period", "evaluation_period"))
    if not (sim[0] == warm[0] <= warm[1] < evaluation[0] <= evaluation[1] == sim[1] and warm[1] + 1 == evaluation[0]):
        raise ValueError("Warmup followed by evaluation must exactly cover simulation")
    return evaluation

def runtime_context(config):
    validate_periods(config)
    edges = {w["subbasin_id"]: w.get("neighbors_downstream", []) for w in config["workers"]}
    ids = set(edges)
    if len(ids) != len(config["workers"]) or any(x not in ids for v in edges.values() for x in v):
        raise ValueError("Invalid configured basin identifiers/topology")
    bounds = config.get("param_ranges", {})
    if {k: tuple(v) for k, v in bounds.items()} != EFFECTIVE_BOUNDS:
        raise ValueError("Runtime profile must match the frozen effective bounds")
    step = 10 ** -DECIMALS["soil_d2"]
    depth_limits = {
        "minimum_representable_strict_gap": step,
        "maximum_feasible_soil_d2": round(min(bounds["soil_d2"][1], bounds["soil_d3"][1] - step), DECIMALS["soil_d2"]),
        "minimum_feasible_soil_d3": round(max(bounds["soil_d3"][0], bounds["soil_d2"][0] + step), DECIMALS["soil_d3"]),
    }
    facts = {"downstream_edges": edges, "effective_parameter_bounds": bounds,
             "parameter_decimal_places": DECIMALS, "depth_constraint": "soil_d3 > soil_d2 after quantization",
             "joint_depth_feasibility": depth_limits,
             "simulation_period": config["simulation_period"], "warmup_period": config["warmup_period"],
             "evaluation_period": config["evaluation_period"], "phase_config": config.get("phase_config", {})}
    weights = config.get('objective_weights', {})
    facts['objective_roles'] = {
        'Leader_J': {'formula': 'mean_nse_weight * mean(station NSE) - consistency_weight * penalty',
                     'mean_nse_weight': weights.get('mean_nse', .90),
                     'consistency_weight': weights.get('consistency', .10),
                     'penalty': 'mean(abs(peak_ratio-1), abs(lowflow_ratio-1)) over all configured stations',
                     'direction': 'maximize'},
        'local_acceptance': 'candidate NSE versus measured center in the same upstream background',
        'representative_round': 'highest mean station NSE among complete accepted joint vectors',
    }
    from .prompt_source import render_static
    return render_static("runtime_config_facts", protocol=config.get("static_input_protocol"),
                         facts_json=json.dumps(facts, sort_keys=True))

def score_period(obs, sim, start, end):
    # Use the same objective and coverage definitions as the conventional optimizers.
    from . import vic_runner
    if start > end:
        raise ValueError("Invalid evaluation interval")
    expected = {start + dt.timedelta(days=i) for i in range((end-start).days+1)}
    if not expected <= sim.keys():
        raise ValueError("Incomplete simulated date coverage")
    if any(not is_finite(sim[d]) for d in expected):
        raise ValueError("Nonfinite simulated discharge")
    selected = {d: float(obs[d]) for d in expected & obs.keys() if is_finite(obs[d]) and obs[d] >= 0}
    if len(selected) < 10:
        raise ValueError("Insufficient valid observations")
    simulated = {d: float(sim[d]) for d in selected}
    values = vic_runner.compute_metrics(selected, simulated)
    metrics = dict(zip(("NSE", "RMSE", "PBIAS", "KGE", "peak_ratio", "lowflow_ratio"), values))
    if not all(is_finite(metrics[k]) for k in ("NSE", "RMSE", "PBIAS")):
        raise ValueError("Invalid primary evaluation metric")
    status = "complete_daily_observations"
    if len(selected) == len(expected):
        metrics.update(vic_runner.compute_signatures(selected, simulated))
    else:
        status = "temporal_signatures_omitted_for_observation_gaps"
    metrics["evaluation_start"] = start.isoformat()
    metrics["evaluation_end"] = end.isoformat()
    metrics["n_scored_days"] = len(selected)
    metrics["diagnostic_status"] = status
    return metrics

def global_score(worker_results, workers, weights):
    nan4 = (float("nan"),) * 4
    expected = {w["subbasin_id"] for w in workers}
    if not expected or len(worker_results) != len(expected):
        return nan4
    ids = [r.get("subbasin_id") for r in worker_results]
    if len(set(ids)) != len(ids) or set(ids) != expected:
        return nan4
    if any(r.get("ok") is not True or not is_finite(r.get("metrics", {}).get("NSE")) for r in worker_results):
        return nan4
    outlet_ids = {w["subbasin_id"] for w in workers if w.get("is_outlet")}
    nses = [r["metrics"]["NSE"] for r in worker_results]
    outlet = [r["metrics"]["NSE"] for r in worker_results if r["subbasin_id"] in outlet_ids]
    mean = sum(nses) / len(nses)
    mean_outlet = sum(outlet) / len(outlet) if outlet else float("nan")
    ratios = [r["metrics"].get(k) for r in worker_results for k in ("peak_ratio", "lowflow_ratio")]
    # Undefined penalty terms must not improve J by disappearing from its denominator.
    consistency = sum(abs(v-1) for v in ratios) / len(ratios) if all(is_finite(v) for v in ratios) else float("nan")
    coefficient = weights.get("consistency", .10)
    penalty = coefficient * consistency if coefficient else 0.0
    return weights.get("mean_nse", .90) * mean - penalty, mean_outlet, mean, consistency
