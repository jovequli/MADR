"""Render numerical diagnostic evidence and optional historical-policy diagnoses.

The raw_metrics rendering omits code-authored parameter directions and responsibility weights."""
import math
from .prompt_source import get_text

MODES = ("historical_policy",)
METRIC_KEYS = (
    "NSE", "RMSE", "PBIAS", "KGE", "peak_ratio", "lowflow_ratio",
    "peak_timing_bias_days", "peak_magnitude_bias_pct", "recession_k_ratio",
    "baseflow_index_bias_pct",
)


def validate_mode(mode):
    if mode not in MODES:
        raise ValueError(f"Unknown diagnostic_protocol.mode: {mode!r}")
    return mode


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def fmt(value):
    number = finite(value)
    return "unavailable" if number is None else f"{number:.6g}"


def recession_status(value):
    value = finite(value)
    if value is None:
        return "unknown"
    return "too_fast" if value > 1.3 else ("too_slow" if value < 0.7 else "ok")


def metric_lines(metrics, static_input_protocol=None):
    metrics = metrics or {}
    lines = [get_text('metric_measurement_header', protocol=static_input_protocol)]
    for offset in range(0, len(METRIC_KEYS), 3):
        lines.append("  " + " | ".join(f"{k}={fmt(metrics.get(k))}" for k in METRIC_KEYS[offset:offset + 3]))
    missing = [k for k in METRIC_KEYS if finite(metrics.get(k)) is None]
    lines.append("Availability: " + ("UNAVAILABLE: " + ", ".join(missing) if missing else "all listed metrics available"))
    for key in ("diagnostic_status", "evaluation_start", "evaluation_end", "n_scored_days"):
        if metrics.get(key) is not None:
            lines.append(f"  {key}={metrics[key]}")
    return lines


def exploration_statistics(results, static_input_protocol=None):
    """Report empirical group statistics, not 'good directions' or causal effects."""
    lines = [get_text('exploration_statistics_header', protocol=static_input_protocol)]
    keys = ("binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3")
    for sid, result in results.items():
        samples = [s for s in result.get("all_results", []) if finite(s.get("nse")) is not None
                   and s.get("ok", True) is not False]
        if not samples:
            lines.append(f"{sid}: no valid exploration scores")
            continue
        ordered = sorted(samples, key=lambda s: float(s["nse"]), reverse=True)
        k = min(10, len(ordered) // 2)
        prefix = f"{sid}: valid={len(ordered)}, NSE range=[{fmt(ordered[-1]['nse'])},{fmt(ordered[0]['nse'])}]"
        if k == 0:
            lines.append(prefix + "; insufficient samples for non-overlapping groups")
            continue
        pieces = []
        for key in keys:
            means = []
            for group in (ordered[:k], ordered[-k:]):
                values = [finite(s.get("params", {}).get(key)) for s in group]
                values = [v for v in values if v is not None]
                means.append(sum(values) / len(values) if values else None)
            pieces.append(f"{key}=({fmt(means[0])},{fmt(means[1])})")
        lines.append(prefix + f"; parameter means (highest-{k} NSE group, lowest-{k} NSE group): " + "; ".join(pieces))
    return "\n".join(lines)
