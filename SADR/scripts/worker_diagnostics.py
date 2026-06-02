"""
水文诊断引擎 — Worker自主诊断模块

基于水文指标对模拟误差进行归因分析，判断主要问题类型（水量/时间/形态/基流），
并将责任归因到VIC参数、routing参数或上游输入。

参考: 同事Structure B的diagnosis.py + param_knowledge_worker.md中的诊断规则表

Usage:
    from worker_diagnostics import diagnose_round

    result = diagnose_round(
        metrics={"NSE": 0.62, "PBIAS": -12.3, "peak_ratio": 0.85, ...},
        prev_metrics={"NSE": 0.58, ...},  # 上一轮指标(可选)
        context={"is_headwater": True, "upstream_quality": "good"}
    )
    print(result.primary_issue)       # "volume"
    print(result.vic_responsibility)  # 0.9
    print(result.recommendations)     # [{"param": "binfilt", ...}]
"""

import math
import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class DiagnosisResult:
    """
    水文诊断结果。

    Attributes
    ----------
    primary_issue : str
        主要问题类型: "volume"|"timing"|"shape"|"baseflow"|"combined"|"none"
    severity : str
        严重程度: "minor" (NSE>0.65), "moderate" (0.4<NSE≤0.65), "severe" (NSE≤0.4)
    severity_score : float
        严重度数值 [0, 1]，用于playbook优先级计算
    vic_responsibility : float
        VIC参数导致的责任占比 [0, 1]
    routing_responsibility : float
        routing参数导致的责任占比 [0, 1]
    upstream_responsibility : float
        上游输入导致的责任占比 [0, 1]
    reasoning : str
        1-2句诊断解释
    recommendations : list
        排序后的调参建议列表，每项: {param, direction, magnitude, confidence, reason}
    improvement_trend : str
        "improving"|"stagnant"|"declining"，基于prev_metrics对比
    """
    primary_issue: str = "none"
    severity: str = "minor"
    severity_score: float = 0.0
    vic_responsibility: float = 0.0
    routing_responsibility: float = 0.0
    upstream_responsibility: float = 0.0
    reasoning: str = ""
    recommendations: List[Dict[str, Any]] = field(default_factory=list)
    improvement_trend: str = "stagnant"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @property
    def summary(self) -> str:
        """Compact summary for Leader."""
        if self.primary_issue == "none":
            return "no significant issue"
        resp_parts = []
        if self.vic_responsibility > 0.3:
            resp_parts.append(f"VIC {self.vic_responsibility:.0%}")
        if self.routing_responsibility > 0.3:
            resp_parts.append(f"Routing {self.routing_responsibility:.0%}")
        if self.upstream_responsibility > 0.3:
            resp_parts.append(f"Upstream {self.upstream_responsibility:.0%}")
        resp_str = ", ".join(resp_parts) if resp_parts else "unattributed"
        return f"{self.primary_issue} issue, severity={self.severity}, attribution: {resp_str}"


# ============================================================================
# 安全的浮点操作
# ============================================================================

def _safe_float(v, default=0.0) -> float:
    """安全提取浮点值，NaN/None/inf返回默认值。"""
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _safe_abs(v, default=0.0) -> float:
    return abs(_safe_float(v, default))


# ============================================================================
# 核心诊断函数
# ============================================================================

def diagnose_round(
    metrics: Dict[str, Any],
    prev_metrics: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> DiagnosisResult:
    """
    对单轮模拟结果进行水文诊断。

    Parameters
    ----------
    metrics : dict
        当前轮次指标，必须包含NSE，可选: PBIAS, peak_ratio, lowflow_ratio,
        peak_timing_bias_days, recession_k_ratio, baseflow_index_bias_pct,
        peak_magnitude_bias_pct, KGE, RMSE
    prev_metrics : dict, optional
        上一轮指标，用于判断改进趋势
    context : dict, optional
        额外上下文: is_headwater (bool), upstream_quality (str: good/acceptable/poor)

    Returns
    -------
    DiagnosisResult
        包含问题分类、责任归因、建议列表
    """
    try:
        if context is None:
            context = {}
        is_headwater = context.get("is_headwater", True)
        upstream_quality = context.get("upstream_quality", "good")

        # 提取指标
        nse = _safe_float(metrics.get("NSE"), -999)
        pbias = _safe_float(metrics.get("PBIAS"), 0.0)
        peak_ratio = _safe_float(metrics.get("peak_ratio"), 1.0)
        lowflow_ratio = _safe_float(metrics.get("lowflow_ratio"), 1.0)
        peak_timing = _safe_float(metrics.get("peak_timing_bias_days"), 0.0)
        recession_k = _safe_float(metrics.get("recession_k_ratio"), 1.0)
        baseflow_bias = _safe_float(metrics.get("baseflow_index_bias_pct"), 0.0)
        peak_mag_bias = _safe_float(metrics.get("peak_magnitude_bias_pct"), 0.0)

        # Step 1: 严重度分类
        severity, severity_score = _classify_severity(nse)

        # Step 2: 主要问题分类
        primary_issue = _classify_primary_issue(
            pbias, peak_timing, recession_k, baseflow_bias, peak_ratio, lowflow_ratio
        )

        # Step 3: 责任归因
        vic_resp, routing_resp, upstream_resp = _attribute_responsibility(
            primary_issue, is_headwater, upstream_quality
        )

        # Step 4: 生成推理文本
        reasoning = _generate_reasoning(
            primary_issue, pbias, peak_timing, recession_k,
            baseflow_bias, vic_resp, routing_resp
        )

        # Step 5: 生成建议
        recommendations = _generate_recommendations(
            primary_issue, pbias, peak_ratio, lowflow_ratio,
            peak_timing, recession_k, baseflow_bias, peak_mag_bias,
            severity_score
        )

        # Step 6: 改进趋势
        improvement_trend = _assess_trend(nse, prev_metrics)

        return DiagnosisResult(
            primary_issue=primary_issue,
            severity=severity,
            severity_score=severity_score,
            vic_responsibility=vic_resp,
            routing_responsibility=routing_resp,
            upstream_responsibility=upstream_resp,
            reasoning=reasoning,
            recommendations=recommendations,
            improvement_trend=improvement_trend,
        )

    except Exception as e:
        # 安全降级
        return DiagnosisResult(
            reasoning=f"Diagnostic fallback: {type(e).__name__}: {e}",
        )


# ============================================================================
# Step 1: 严重度分类
# ============================================================================

def _classify_severity(nse: float) -> tuple:
    """返回 (severity_str, severity_score)。"""
    if nse == -999 or math.isnan(nse):
        return "severe", 1.0
    if nse > 0.65:
        # minor: score在[0, 0.35]之间线性
        score = max(0.0, 1.0 - nse)  # nse=0.65 → score=0.35, nse=1.0 → score=0.0
        return "minor", score
    elif nse > 0.4:
        # moderate: score在[0.35, 0.6]之间
        score = 1.0 - nse  # nse=0.4 → score=0.6, nse=0.65 → score=0.35
        return "moderate", score
    else:
        # severe: score在[0.6, 1.0]之间
        score = min(1.0, 1.0 - nse)
        return "severe", score


# ============================================================================
# Step 2: 主要问题分类
# ============================================================================

def _classify_primary_issue(
    pbias: float, peak_timing: float, recession_k: float,
    baseflow_bias: float, peak_ratio: float, lowflow_ratio: float
) -> str:
    """
    分类主要问题: volume, timing, shape, baseflow, combined, none

    阈值:
    - volume: |PBIAS| > 10% 或 peak_ratio偏离1.0超过15%
    - timing: |peak_timing_bias| > 2天
    - shape:  recession_k_ratio ∉ [0.7, 1.3]
    - baseflow: |baseflow_index_bias| > 20% 或 lowflow_ratio偏离1.0超过20%
    """
    has_volume = abs(pbias) > 10.0 or abs(peak_ratio - 1.0) > 0.15
    has_timing = abs(peak_timing) > 2.0
    has_shape = recession_k < 0.7 or recession_k > 1.3
    has_baseflow = abs(baseflow_bias) > 20.0 or abs(lowflow_ratio - 1.0) > 0.2

    issues = sum([has_volume, has_timing, has_shape, has_baseflow])

    if issues == 0:
        return "none"
    elif issues == 1:
        if has_volume:
            return "volume"
        elif has_timing:
            return "timing"
        elif has_shape:
            return "shape"
        else:
            return "baseflow"
    else:
        return "combined"


# ============================================================================
# Step 3: 责任归因
# ============================================================================

def _attribute_responsibility(
    primary_issue: str, is_headwater: bool, upstream_quality: str
) -> tuple:
    """
    返回 (vic_resp, routing_resp, upstream_resp)，和为1.0。

    归因规则(源自水文标定最佳实践):
    - volume → VIC 0.9, Routing 0.1
    - timing → VIC 0.1, Routing 0.9
    - shape  → VIC 0.85, Routing 0.15
    - baseflow → VIC 0.8, Routing 0.2
    - combined + 上游差 → Upstream 0.5, VIC 0.3, Routing 0.2
    - combined + 上游好 → VIC 0.5, Routing 0.5
    """
    if primary_issue == "none":
        return 0.0, 0.0, 0.0

    ATTRIBUTION = {
        "volume":   (0.9, 0.1, 0.0),
        "timing":   (0.1, 0.9, 0.0),
        "shape":    (0.85, 0.15, 0.0),
        "baseflow": (0.8, 0.2, 0.0),
    }

    if primary_issue in ATTRIBUTION:
        vic, rout, up = ATTRIBUTION[primary_issue]
    elif primary_issue == "combined":
        if not is_headwater and upstream_quality in ("poor", "acceptable"):
            vic, rout, up = 0.3, 0.2, 0.5
        else:
            vic, rout, up = 0.5, 0.5, 0.0
    else:
        vic, rout, up = 0.33, 0.33, 0.34

    # 归一化
    total = vic + rout + up
    if total > 0:
        vic /= total
        rout /= total
        up /= total

    return round(vic, 2), round(rout, 2), round(up, 2)


# ============================================================================
# Step 4: 推理文本
# ============================================================================

def _generate_reasoning(
    primary_issue: str, pbias: float, peak_timing: float,
    recession_k: float, baseflow_bias: float,
    vic_resp: float, routing_resp: float
) -> str:
    if primary_issue == "none":
        return "Simulation acceptable, no major issue."
    elif primary_issue == "volume":
        if pbias > 0:
            return f"Too wet (PBIAS={pbias:.1f}%), VIC runoff too high, reduce surface runoff."
        else:
            return f"Too dry (PBIAS={pbias:.1f}%), VIC runoff too low, increase surface runoff."
    elif primary_issue == "timing":
        if peak_timing < 0:
            return f"Peak too early ({peak_timing:.1f}d), routing velocity too fast."
        else:
            return f"Peak too late ({peak_timing:.1f}d), routing velocity too slow."
    elif primary_issue == "shape":
        if recession_k > 1.3:
            return f"Recession too fast (k_ratio={recession_k:.2f}), insufficient soil storage."
        else:
            return f"Recession too slow (k_ratio={recession_k:.2f}), excessive soil storage."
    elif primary_issue == "baseflow":
        if baseflow_bias < -20:
            return f"Baseflow too low (bias={baseflow_bias:.1f}%), insufficient deep soil release."
        else:
            return f"Baseflow too high (bias={baseflow_bias:.1f}%), excessive deep soil release."
    else:  # combined
        return (
            f"Combined issue: VIC resp.={vic_resp:.0%}, Routing resp.={routing_resp:.0%}. "
            f"PBIAS={pbias:.1f}%, peak_timing={peak_timing:.1f}d."
        )


# ============================================================================
# Step 5: 生成建议
# ============================================================================

def _generate_recommendations(
    primary_issue: str, pbias: float, peak_ratio: float,
    lowflow_ratio: float, peak_timing: float, recession_k: float,
    baseflow_bias: float, peak_mag_bias: float, severity_score: float
) -> List[Dict]:
    """
    Generate parameter adjustment recommendations based on diagnosis.

    Each rec: {param, direction, magnitude, confidence, reason, category}
    category: "vic" or "routing"
    """
    recs = []

    # === VIC recommendations ===

    # Peak too high → decrease binfilt
    if peak_ratio > 1.05:
        mag = "large" if peak_ratio > 1.2 else "medium" if peak_ratio > 1.1 else "small"
        recs.append({
            "param": "binfilt", "direction": "decrease", "magnitude": mag,
            "confidence": min(0.95, 0.5 + abs(peak_ratio - 1.0)),
            "reason": f"peak_ratio={peak_ratio:.2f}>1.05, decrease binfilt to reduce peak",
            "category": "vic"
        })

    # Peak too low → increase binfilt
    if peak_ratio < 0.95:
        mag = "large" if peak_ratio < 0.8 else "medium" if peak_ratio < 0.9 else "small"
        recs.append({
            "param": "binfilt", "direction": "increase", "magnitude": mag,
            "confidence": min(0.95, 0.5 + abs(peak_ratio - 1.0)),
            "reason": f"peak_ratio={peak_ratio:.2f}<0.95, increase binfilt to raise peak",
            "category": "vic"
        })

    # Low flow too low → increase Ds/soil_d3
    if lowflow_ratio < 0.9:
        recs.append({
            "param": "Ds", "direction": "increase", "magnitude": "medium",
            "confidence": 0.7,
            "reason": f"lowflow_ratio={lowflow_ratio:.2f}<0.9, increase Ds for baseflow",
            "category": "vic"
        })
        recs.append({
            "param": "soil_d3", "direction": "increase", "magnitude": "small",
            "confidence": 0.6,
            "reason": f"lowflow too low, increase soil_d3 for deep storage",
            "category": "vic"
        })

    # Low flow too high → decrease Ds
    if lowflow_ratio > 1.1:
        recs.append({
            "param": "Ds", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.7,
            "reason": f"lowflow_ratio={lowflow_ratio:.2f}>1.1, decrease Ds to reduce baseflow",
            "category": "vic"
        })

    # PBIAS too wet → increase soil_d2 storage
    if pbias > 10.0:
        recs.append({
            "param": "soil_d2", "direction": "increase", "magnitude": "medium",
            "confidence": 0.75,
            "reason": f"PBIAS={pbias:.1f}% too wet, increase soil_d2 to store more",
            "category": "vic"
        })

    # PBIAS too dry → decrease soil_d2
    if pbias < -10.0:
        recs.append({
            "param": "soil_d2", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.75,
            "reason": f"PBIAS={pbias:.1f}% too dry, decrease soil_d2 to release more",
            "category": "vic"
        })

    # Recession too fast → increase soil_d3 + Ws
    if recession_k > 1.3:
        recs.append({
            "param": "soil_d3", "direction": "increase", "magnitude": "medium",
            "confidence": 0.8,
            "reason": f"recession_k={recession_k:.2f}>1.3, increase soil_d3 to slow recession",
            "category": "vic"
        })
        recs.append({
            "param": "Ws", "direction": "increase", "magnitude": "small",
            "confidence": 0.6,
            "reason": "Recession too fast, increase Ws to delay nonlinear baseflow",
            "category": "vic"
        })

    # Recession too slow → decrease soil_d3 + increase Ds
    if recession_k < 0.7:
        recs.append({
            "param": "soil_d3", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.8,
            "reason": f"recession_k={recession_k:.2f}<0.7, decrease soil_d3 to speed recession",
            "category": "vic"
        })
        recs.append({
            "param": "Ds", "direction": "increase", "magnitude": "small",
            "confidence": 0.6,
            "reason": "Recession too slow, increase Ds to speed baseflow drainage",
            "category": "vic"
        })

    # Baseflow too low → increase soil_d3
    if baseflow_bias < -20.0:
        recs.append({
            "param": "soil_d3", "direction": "increase", "magnitude": "medium",
            "confidence": 0.75,
            "reason": f"baseflow_bias={baseflow_bias:.1f}% too low, increase deep storage",
            "category": "vic"
        })

    # === Routing recommendations ===

    # Peak too early → decrease velocity
    if peak_timing < -2.0:
        mag = "large" if peak_timing < -5 else "medium" if peak_timing < -3 else "small"
        recs.append({
            "param": "velocity", "direction": "decrease", "magnitude": mag,
            "confidence": min(0.9, 0.5 + abs(peak_timing) * 0.1),
            "reason": f"Peak {peak_timing:.1f}d early, decrease velocity to delay peak",
            "category": "routing"
        })

    # Peak too late → increase velocity
    if peak_timing > 2.0:
        mag = "large" if peak_timing > 5 else "medium" if peak_timing > 3 else "small"
        recs.append({
            "param": "velocity", "direction": "increase", "magnitude": mag,
            "confidence": min(0.9, 0.5 + abs(peak_timing) * 0.1),
            "reason": f"Peak {peak_timing:.1f}d late, increase velocity to advance peak",
            "category": "routing"
        })

    # Peak too sharp → increase diffusivity
    if peak_mag_bias > 20.0 and abs(pbias) < 10.0:
        recs.append({
            "param": "diffusivity", "direction": "increase", "magnitude": "small",
            "confidence": 0.7,
            "reason": f"Peak mag bias={peak_mag_bias:.1f}% but volume ok, increase diffusivity to smooth",
            "category": "routing"
        })

    # Peak too flat → decrease diffusivity
    if peak_mag_bias < -20.0 and abs(pbias) < 10.0:
        recs.append({
            "param": "diffusivity", "direction": "decrease", "magnitude": "small",
            "confidence": 0.7,
            "reason": f"Peak mag bias={peak_mag_bias:.1f}% but volume ok, decrease diffusivity to sharpen",
            "category": "routing"
        })

    # Sort by confidence × severity
    severity_weight = 1.0 + severity_score  # [1.0, 2.0]
    for rec in recs:
        rec["priority"] = rec["confidence"] * severity_weight

    recs.sort(key=lambda r: r["priority"], reverse=True)

    return recs[:6]


# ============================================================================
# Step 6: 改进趋势
# ============================================================================

def _assess_trend(nse: float, prev_metrics: Optional[Dict]) -> str:
    """判断改进趋势。"""
    if prev_metrics is None:
        return "stagnant"
    prev_nse = _safe_float(prev_metrics.get("NSE"), -999)
    if prev_nse == -999 or nse == -999:
        return "stagnant"
    delta = nse - prev_nse
    if delta > 0.005:
        return "improving"
    elif delta < -0.005:
        return "declining"
    else:
        return "stagnant"
