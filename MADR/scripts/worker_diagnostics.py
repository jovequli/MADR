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
        主要问题类型: "volume"|"timing"|"shape"|"baseflow"|"combined"|"none"|"unknown"
    severity : str
        严重程度: "minor" (NSE>0.65), "moderate" (0.4<NSE≤0.65), "severe" (NSE≤0.4)
    severity_score : float
        严重度数值 [0, 1]，用于playbook优先级计算
    vic_responsibility : float
        VIC参数导致的责任占比 [0, 1]；vic_cama_flood下指VIC陆面阶段；
        swat无独立routing模块，此项为NaN(不划分)
    routing_responsibility : float
        routing参数导致的责任占比 [0, 1]；vic_routing指Lohmann routing，
        vic_cama_flood指CaMa-Flood河道阶段；swat为NaN(不划分)
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
        if self.primary_issue == "unknown":
            return "insufficient data for diagnosis"
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
        peak_magnitude_bias_pct, KGE, RMSE, timestep ("daily"|"monthly")
    prev_metrics : dict, optional
        上一轮指标，用于判断改进趋势
    context : dict, optional
        额外上下文: is_headwater (bool), upstream_quality (str: good/acceptable/poor),
        model_combination (str: vic_routing/vic_cama_flood/swat, 默认vic_routing),
        current_params (dict, 可选): 被诊断候选/中心的当前参数值，用于证据门控
        规则(如swat的perco规则需要已验证的deep_seep值)；缺省=旧行为，
        证据门控的建议不输出

    Returns
    -------
    DiagnosisResult
        包含问题分类、责任归因、建议列表

    Notes
    -----
    Timestep handling (daily vs monthly observations):

    - When obs are monthly, the runners aggregate sim to monthly means before
      computing metrics and tag ``metrics["timestep"] = "monthly"``.
    - At monthly scale the timing, recession and baseflow-index signatures
      (peak_timing_bias_days, recession_k_ratio, baseflow_index_bias_pct)
      cannot be estimated from monthly means: the runners emit NaN for them,
      so they fall through the "insufficient data" path (no timing, shape or
      baseflow-index classification, no recommendation from those rules).
      peak_timing is additionally forced to NaN below as a guard: on a
      monthly series one index step is one MONTH, so the day thresholds
      (|bias|>2) and the velocity magnitude tiers would misread it.
    - Volume metrics (NSE, PBIAS, peak_ratio, lowflow_ratio) survive monthly
      aggregation, so the volume and low-flow rules below stay valid at
      monthly scale.
    - The SWAT+ monthly path computes no signatures at all, so those metrics
      are absent there and fall through the same NaN handling. Daily behavior
      is unchanged: without ``timestep == "monthly"`` no rule is altered.
    """
    try:
        if context is None:
            context = {}
        is_headwater = context.get("is_headwater", True)
        upstream_quality = context.get("upstream_quality", "good")
        model_combination = str(context.get("model_combination", "vic_routing"))
        current_params = context.get("current_params") or {}

        # 提取指标
        nse = _safe_float(metrics.get("NSE"), -999)
        pbias = _safe_float(metrics.get("PBIAS"), float("nan"))
        peak_ratio = _safe_float(metrics.get("peak_ratio"), float("nan"))
        lowflow_ratio = _safe_float(metrics.get("lowflow_ratio"), float("nan"))
        peak_timing = _safe_float(metrics.get("peak_timing_bias_days"), float("nan"))
        recession_k = _safe_float(metrics.get("recession_k_ratio"), float("nan"))
        baseflow_bias = _safe_float(metrics.get("baseflow_index_bias_pct"), float("nan"))
        peak_mag_bias = _safe_float(metrics.get("peak_magnitude_bias_pct"), float("nan"))

        # 月尺度下peak_timing以“月”为单位，日尺度timing规则不适用(见Notes)
        if str(metrics.get("timestep", "daily")).lower() == "monthly":
            peak_timing = float("nan")

        # Step 1: 严重度分类
        severity, severity_score = _classify_severity(nse)

        # Step 2: 主要问题分类
        primary_issue = _classify_primary_issue(
            pbias, peak_timing, recession_k, baseflow_bias, peak_ratio, lowflow_ratio
        )

        # Step 3: 责任归因
        vic_resp, routing_resp, upstream_resp = _attribute_responsibility(
            primary_issue, is_headwater, upstream_quality, model_combination
        )

        # Step 4: 生成推理文本
        reasoning = _generate_reasoning(
            primary_issue, pbias, peak_timing, recession_k,
            baseflow_bias, vic_resp, routing_resp, model_combination
        )

        # Step 5: 生成建议
        recommendations = _generate_recommendations(
            primary_issue, pbias, peak_ratio, lowflow_ratio,
            peak_timing, recession_k, baseflow_bias, peak_mag_bias,
            severity_score, model_combination, current_params
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
            primary_issue="unknown",
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
    分类主要问题: volume, timing, shape, baseflow, combined, none, unknown

    阈值:
    - volume: |PBIAS| > 10% 或 peak_ratio偏离1.0超过15%
    - timing: |peak_timing_bias| > 2天
    - shape:  recession_k_ratio ∉ [0.7, 1.3]
    - baseflow: |baseflow_index_bias| > 20% 或 lowflow_ratio偏离1.0超过20%
    - unknown: 全部指标缺失(NaN)，证据不足

    NaN指标不触发任何规则(与NaN比较恒为False)。
    """
    has_volume = abs(pbias) > 10.0 or abs(peak_ratio - 1.0) > 0.15
    has_timing = abs(peak_timing) > 2.0
    has_shape = recession_k < 0.7 or recession_k > 1.3
    has_baseflow = abs(baseflow_bias) > 20.0 or abs(lowflow_ratio - 1.0) > 0.2

    issues = sum([has_volume, has_timing, has_shape, has_baseflow])

    if issues == 0:
        if all(math.isnan(v) for v in
               (pbias, peak_timing, recession_k, baseflow_bias,
                peak_ratio, lowflow_ratio)):
            return "unknown"
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
    primary_issue: str, is_headwater: bool, upstream_quality: str,
    model_combination: str = "vic_routing",
) -> tuple:
    """
    返回 (vic_resp, routing_resp, upstream_resp)，和为1.0。

    组合语义:
    - vic_routing: vic_resp=VIC陆面参数, routing_resp=Lohmann routing参数
    - vic_cama_flood: 数值划分与vic_routing相同，但vic_resp指VIC陆面阶段、
      routing_resp指CaMa-Flood河道阶段
    - swat: SWAT+无独立routing模块，不做陆面/routing划分，
      vic_resp与routing_resp返回NaN(仅保留upstream_resp)；
      消费方须容忍NaN(与NaN比较恒为False)

    归因规则(源自水文标定最佳实践):
    - volume → VIC 0.9, Routing 0.1
    - timing → VIC 0.1, Routing 0.9
    - shape  → VIC 0.85, Routing 0.15
    - baseflow → VIC 0.8, Routing 0.2
    - combined + 上游差 → Upstream 0.5, VIC 0.3, Routing 0.2
    - combined + 上游好 → VIC 0.5, Routing 0.5
    """
    if primary_issue in ("none", "unknown"):
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

    # swat: 无独立routing模块，不划分陆面/routing责任(见docstring)
    if model_combination in ("swat", "wflow"):
        return float("nan"), float("nan"), round(up, 2)

    return round(vic, 2), round(rout, 2), round(up, 2)


# ============================================================================
# Step 4: 推理文本
# ============================================================================

def _generate_reasoning(
    primary_issue: str, pbias: float, peak_timing: float,
    recession_k: float, baseflow_bias: float,
    vic_resp: float, routing_resp: float,
    model_combination: str = "vic_routing",
) -> str:
    # 按模型组合调整措辞(vic_routing措辞与原版逐字一致)
    land = "SWAT+" if model_combination == "swat" else ("wflow_sbm" if model_combination == "wflow" else "VIC")
    if model_combination == "swat":
        too_fast, too_slow = "surface lag too short", "surface lag too long"
    elif model_combination == "wflow":
        too_fast, too_slow = "kinematic wave too fast (N too low)", "kinematic wave too slow (N too high)"
    elif model_combination == "vic_cama_flood":
        too_fast, too_slow = "channel wave too fast", "channel wave too slow"
    else:
        too_fast, too_slow = "routing velocity too fast", "routing velocity too slow"
    if primary_issue == "unknown":
        return "Insufficient data: diagnostic metrics missing, cannot classify issue."
    elif primary_issue == "none":
        missing = [name for name, v in (
            ("PBIAS", pbias), ("peak_timing", peak_timing),
            ("recession_k", recession_k), ("baseflow_bias", baseflow_bias),
        ) if math.isnan(v)]
        if missing:
            return (f"No issue found in available metrics; "
                    f"insufficient data for: {', '.join(missing)}.")
        return "Simulation acceptable, no major issue."
    elif primary_issue == "volume":
        # Canonical convention (all models): PBIAS = 100*sum(sim-obs)/sum(obs), positive = too wet
        if math.isnan(pbias):
            return "Volume issue (peak_ratio deviates from 1.0), PBIAS unavailable (insufficient data)."
        elif pbias > 0:
            return f"Too wet (PBIAS={pbias:.1f}%), {land} runoff too high, reduce surface runoff."
        else:
            return f"Too dry (PBIAS={pbias:.1f}%), {land} runoff too low, increase surface runoff."
    elif primary_issue == "timing":
        if peak_timing < 0:
            return f"Peak too early ({peak_timing:.1f}d), {too_fast}."
        else:
            return f"Peak too late ({peak_timing:.1f}d), {too_slow}."
    elif primary_issue == "shape":
        if model_combination == "swat":
            # SWAT+退水由地下水模块控制(alpha/flo_min)，而非土壤储水
            if recession_k > 1.3:
                return (f"Recession too fast (k_ratio={recession_k:.2f}), "
                        f"aquifer releases too quickly (alpha too high / flo_min too low).")
            else:
                return (f"Recession too slow (k_ratio={recession_k:.2f}), "
                        f"aquifer releases too slowly (alpha too low / flo_min too high).")
        if recession_k > 1.3:
            return f"Recession too fast (k_ratio={recession_k:.2f}), insufficient soil storage."
        else:
            return f"Recession too slow (k_ratio={recession_k:.2f}), excessive soil storage."
    elif primary_issue == "baseflow":
        deep_source = "aquifer return flow" if model_combination == "swat" else "deep soil release"
        if math.isnan(baseflow_bias):
            return "Baseflow issue (lowflow_ratio deviates from 1.0), baseflow index unavailable (insufficient data)."
        elif baseflow_bias < -20:
            return f"Baseflow too low (bias={baseflow_bias:.1f}%), insufficient {deep_source}."
        else:
            return f"Baseflow too high (bias={baseflow_bias:.1f}%), excessive {deep_source}."
    else:  # combined
        pbias_str = "n/a" if math.isnan(pbias) else f"{pbias:.1f}%"
        timing_str = "n/a" if math.isnan(peak_timing) else f"{peak_timing:.1f}d"
        if model_combination == "swat":
            # SWAT+一体化模型无VIC/routing责任划分(resp为NaN)
            return (
                f"Combined issue: multiple signatures off (SWAT+ integrated model, no land/routing split). "
                f"PBIAS={pbias_str}, peak_timing={timing_str}."
            )
        if model_combination == "wflow":
            # wflow_sbm一体化模型(SBM土壤 + kinematic-wave汇流)无VIC/routing责任划分
            return (
                f"Combined issue: multiple signatures off (wflow_sbm integrated SBM+kinematic-wave model, no land/routing split). "
                f"PBIAS={pbias_str}, peak_timing={timing_str}."
            )
        return (
            f"Combined issue: VIC resp.={vic_resp:.0%}, Routing resp.={routing_resp:.0%}. "
            f"PBIAS={pbias_str}, peak_timing={timing_str}."
        )


# ============================================================================
# Step 5: 生成建议
# ============================================================================

def _wflow_recommendations(
    pbias: float, peak_ratio: float, lowflow_ratio: float,
    peak_timing: float, recession_k: float, baseflow_bias: float,
    peak_mag_bias: float,
    current_params: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """wflow_sbm parameter rules (integrated SBM soil + kinematic-wave routing).

    Signatures map to wflow params in the layered order: KsatHorFrac / KsatVer
    set baseflow volume, f + SoilThickness set the recession / low-flow, N +
    N_River set peak magnitude & timing. Manning N must NOT compensate for
    runoff / groundwater errors -- fix volume and baseflow with the soil
    parameters first; adjust N / N_River only for shape and timing.
    KsatHorFrac and KsatVer are log-sampled.
    """
    recs: List[Dict] = []

    if peak_ratio > 1.05:
        mag = "large" if peak_ratio > 1.2 else "medium" if peak_ratio > 1.1 else "small"
        recs.append({"param": "N", "direction": "increase", "magnitude": mag,
            "confidence": min(0.9, 0.4 + abs(peak_ratio - 1.0)),
            "reason": f"peak_ratio={peak_ratio:.2f}>1.05, increase land Manning N to attenuate the flood peak",
            "category": "wflow"})

    if peak_ratio < 0.95:
        mag = "large" if peak_ratio < 0.8 else "medium" if peak_ratio < 0.9 else "small"
        recs.append({"param": "N", "direction": "decrease", "magnitude": mag,
            "confidence": min(0.9, 0.4 + abs(peak_ratio - 1.0)),
            "reason": f"peak_ratio={peak_ratio:.2f}<0.95, decrease land Manning N to sharpen the peak (only once volume is right)",
            "category": "wflow"})

    if lowflow_ratio < 0.9 or (not math.isnan(baseflow_bias) and baseflow_bias < -20):
        recs.append({"param": "KsatHorFrac", "direction": "increase", "magnitude": "medium",
            "confidence": 0.75,
            "reason": f"lowflow_ratio={lowflow_ratio:.2f}, increase KsatHorFrac (log) to route more lateral subsurface flow to the stream",
            "category": "wflow"})
        recs.append({"param": "SoilThickness", "direction": "increase", "magnitude": "small",
            "confidence": 0.6,
            "reason": "low flows too weak, increase SoilThickness to add storage that sustains dry-season baseflow",
            "category": "wflow"})

    if lowflow_ratio > 1.1:
        recs.append({"param": "KsatHorFrac", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.7,
            "reason": f"lowflow_ratio={lowflow_ratio:.2f}>1.1, decrease KsatHorFrac (log) to reduce excess baseflow",
            "category": "wflow"})

    if pbias > 10.0:
        recs.append({"param": "SoilThickness", "direction": "increase", "magnitude": "medium",
            "confidence": 0.65,
            "reason": f"PBIAS={pbias:.1f}% too wet, increase SoilThickness so more water is stored/evaporated instead of running off",
            "category": "wflow"})

    if pbias < -10.0:
        recs.append({"param": "KsatVer", "direction": "increase", "magnitude": "medium",
            "confidence": 0.65,
            "reason": f"PBIAS={pbias:.1f}% too dry, increase KsatVer (log) so more precip becomes subsurface flow instead of ET loss",
            "category": "wflow"})
        recs.append({"param": "SoilThickness", "direction": "decrease", "magnitude": "small",
            "confidence": 0.5,
            "reason": "too dry, decrease SoilThickness to cut ET storage and release more water to streamflow",
            "category": "wflow"})

    if not math.isnan(recession_k) and recession_k > 1.3:
        recs.append({"param": "f", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.7,
            "reason": f"recession k_ratio={recession_k:.2f}>1.3 too fast, decrease f so KsatVer decays more slowly with depth and the recession lengthens",
            "category": "wflow"})
        recs.append({"param": "SoilThickness", "direction": "increase", "magnitude": "small",
            "confidence": 0.55,
            "reason": "recession too fast, increase SoilThickness for more storage to sustain the recession",
            "category": "wflow"})

    if not math.isnan(recession_k) and recession_k < 0.7:
        recs.append({"param": "f", "direction": "increase", "magnitude": "medium",
            "confidence": 0.7,
            "reason": f"recession k_ratio={recession_k:.2f}<0.7 too slow, increase f so KsatVer decays faster with depth and the recession sharpens",
            "category": "wflow"})

    if not math.isnan(peak_timing):
        if peak_timing < -0.5:
            recs.append({"param": "N_River", "direction": "increase", "magnitude": "small",
                "confidence": 0.55,
                "reason": f"peak {peak_timing:.1f}d too early, increase river Manning N_River to slow the channel wave",
                "category": "wflow"})
        elif peak_timing > 0.5:
            recs.append({"param": "N_River", "direction": "decrease", "magnitude": "small",
                "confidence": 0.55,
                "reason": f"peak {peak_timing:.1f}d too late, decrease river Manning N_River to speed the channel wave",
                "category": "wflow"})

    return recs


def _generate_recommendations(
    primary_issue: str, pbias: float, peak_ratio: float,
    lowflow_ratio: float, peak_timing: float, recession_k: float,
    baseflow_bias: float, peak_mag_bias: float, severity_score: float,
    model_combination: str = "vic_routing",
    current_params: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """
    Generate parameter adjustment recommendations based on diagnosis.

    Each rec: {param, direction, magnitude, confidence, reason, category}
    category: "vic"/"routing" for the VIC combinations, "swat" for SWAT+

    The same diagnostic conditions map to a per-combination parameter menu:
    - vic_routing: VIC soil params + Lohmann velocity/diffusivity
    - vic_cama_flood: VIC soil params + CaMa channel Hc/Hmin/Wc/Wmin
    - swat: SWAT+ HRU params (integrated routing, no separate routing phase)

    current_params (the candidate/center being diagnosed) is consumed only by
    the swat menu for evidence-gated rules; the VIC menus ignore it.
    """
    if model_combination == "swat":
        recs = _swat_recommendations(
            pbias, peak_ratio, lowflow_ratio, peak_timing,
            recession_k, baseflow_bias, peak_mag_bias, current_params
        )
    elif model_combination == "wflow":
        recs = _wflow_recommendations(
            pbias, peak_ratio, lowflow_ratio, peak_timing,
            recession_k, baseflow_bias, peak_mag_bias, current_params
        )
    else:
        recs = _vic_soil_recommendations(
            pbias, peak_ratio, lowflow_ratio, recession_k, baseflow_bias
        )
        if model_combination == "vic_cama_flood":
            recs += _cama_channel_recommendations(
                pbias, peak_timing, recession_k, baseflow_bias, peak_mag_bias
            )
        else:
            recs += _lohmann_routing_recommendations(
                pbias, peak_timing, peak_mag_bias
            )

    # Sort by confidence × severity
    severity_weight = 1.0 + severity_score  # [1.0, 2.0]
    for rec in recs:
        rec["priority"] = rec["confidence"] * severity_weight

    recs.sort(key=lambda r: r["priority"], reverse=True)

    return recs[:6]


def _vic_soil_recommendations(
    pbias: float, peak_ratio: float, lowflow_ratio: float,
    recession_k: float, baseflow_bias: float
) -> List[Dict]:
    """VIC soil-parameter rules (shared by vic_routing and vic_cama_flood)."""
    recs = []

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

    return recs


def _lohmann_routing_recommendations(
    pbias: float, peak_timing: float, peak_mag_bias: float
) -> List[Dict]:
    """Lohmann routing rules (vic_routing)."""
    recs = []

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

    return recs


def _cama_channel_recommendations(
    pbias: float, peak_timing: float, recession_k: float,
    baseflow_bias: float, peak_mag_bias: float
) -> List[Dict]:
    """CaMa-Flood channel geometry rules (vic_cama_flood).

    Directions follow prompts/vic_cama_flood/param_knowledge_leader.md:
    higher Hc/Hmin → deeper channel → slower wave; higher Wc/Wmin → more
    channel storage → attenuated peaks and sustained low flow.
    """
    recs = []

    # Peak too early → channel too fast → increase Hc (+ Hmin for headwaters)
    if peak_timing < -2.0:
        mag = "large" if peak_timing < -5 else "medium" if peak_timing < -3 else "small"
        recs.append({
            "param": "Hc", "direction": "increase", "magnitude": mag,
            "confidence": min(0.9, 0.5 + abs(peak_timing) * 0.1),
            "reason": f"Peak {peak_timing:.1f}d early, increase Hc to slow channel wave",
            "category": "routing"
        })
        recs.append({
            "param": "Hmin", "direction": "increase", "magnitude": "small",
            "confidence": 0.6,
            "reason": "Peak early, increase Hmin to slow headwater streams",
            "category": "routing"
        })

    # Peak too late → channel too slow → decrease Hc (+ Hmin)
    if peak_timing > 2.0:
        mag = "large" if peak_timing > 5 else "medium" if peak_timing > 3 else "small"
        recs.append({
            "param": "Hc", "direction": "decrease", "magnitude": mag,
            "confidence": min(0.9, 0.5 + abs(peak_timing) * 0.1),
            "reason": f"Peak {peak_timing:.1f}d late, decrease Hc to speed channel wave",
            "category": "routing"
        })
        recs.append({
            "param": "Hmin", "direction": "decrease", "magnitude": "small",
            "confidence": 0.6,
            "reason": "Peak late, decrease Hmin to speed headwater streams",
            "category": "routing"
        })

    # Peak too sharp → increase Wc (more channel storage, attenuation)
    if peak_mag_bias > 20.0 and abs(pbias) < 10.0:
        recs.append({
            "param": "Wc", "direction": "increase", "magnitude": "small",
            "confidence": 0.7,
            "reason": f"Peak mag bias={peak_mag_bias:.1f}% but volume ok, increase Wc to attenuate",
            "category": "routing"
        })

    # Peak too flat → decrease Wc
    if peak_mag_bias < -20.0 and abs(pbias) < 10.0:
        recs.append({
            "param": "Wc", "direction": "decrease", "magnitude": "small",
            "confidence": 0.7,
            "reason": f"Peak mag bias={peak_mag_bias:.1f}% but volume ok, decrease Wc to sharpen",
            "category": "routing"
        })

    # Recession too fast → increase Wmin (channel-side assist to soil_d3)
    if recession_k > 1.3:
        recs.append({
            "param": "Wmin", "direction": "increase", "magnitude": "small",
            "confidence": 0.6,
            "reason": f"recession_k={recession_k:.2f}>1.3, increase Wmin to prolong small-stream release",
            "category": "routing"
        })

    # Recession too slow → decrease Wmin
    if recession_k < 0.7:
        recs.append({
            "param": "Wmin", "direction": "decrease", "magnitude": "small",
            "confidence": 0.6,
            "reason": f"recession_k={recession_k:.2f}<0.7, decrease Wmin to speed small-stream release",
            "category": "routing"
        })

    # Baseflow too low → increase Wmin to keep small streams populated
    if baseflow_bias < -20.0:
        recs.append({
            "param": "Wmin", "direction": "increase", "magnitude": "small",
            "confidence": 0.55,
            "reason": f"baseflow_bias={baseflow_bias:.1f}% too low, increase Wmin to sustain low flow",
            "category": "routing"
        })

    return recs


def _swat_recommendations(
    pbias: float, peak_ratio: float, lowflow_ratio: float,
    peak_timing: float, recession_k: float, baseflow_bias: float,
    peak_mag_bias: float,
    current_params: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """SWAT+ HRU parameter rules (swat combination, integrated routing).

    Directions follow prompts/swat/param_knowledge_leader.md under the
    canonical PBIAS = 100*sum(sim-obs)/sum(obs) convention (positive = wet).
    cn2 and awc are pctchg params: direction refers to the sign of the pctchg.
    current_params (candidate being diagnosed) gates the perco rule: without
    a verified low deep_seep no structured perco rec is emitted.
    """
    recs = []

    # Peak too high → decrease cn2 (less surface runoff), increase surlag
    if peak_ratio > 1.05:
        mag = "large" if peak_ratio > 1.2 else "medium" if peak_ratio > 1.1 else "small"
        recs.append({
            "param": "cn2", "direction": "decrease", "magnitude": mag,
            "confidence": min(0.95, 0.5 + abs(peak_ratio - 1.0)),
            "reason": f"peak_ratio={peak_ratio:.2f}>1.05, decrease cn2 (pctchg) to reduce peak",
            "category": "swat"
        })
        recs.append({
            "param": "surlag", "direction": "increase", "magnitude": "small",
            "confidence": 0.6,
            "reason": "Peaks too high, increase surlag to lag surface runoff",
            "category": "swat"
        })

    # Peak too low → increase cn2, decrease surlag
    if peak_ratio < 0.95:
        mag = "large" if peak_ratio < 0.8 else "medium" if peak_ratio < 0.9 else "small"
        recs.append({
            "param": "cn2", "direction": "increase", "magnitude": mag,
            "confidence": min(0.95, 0.5 + abs(peak_ratio - 1.0)),
            "reason": f"peak_ratio={peak_ratio:.2f}<0.95, increase cn2 (pctchg) to raise peak",
            "category": "swat"
        })
        recs.append({
            "param": "surlag", "direction": "decrease", "magnitude": "small",
            "confidence": 0.6,
            "reason": "Peaks too low, decrease surlag to concentrate surface runoff",
            "category": "swat"
        })

    # Low flow too low → groundwater menu per the PERCO / DRY-BIAS RULE in
    # prompts/swat/param_knowledge_leader.md: check alpha / flo_min /
    # deep_seep / revap_co FIRST.
    # NOTE (empirical, Yellow 1981-1990 monthly run, 2026-07): "increase perco
    # to recharge the aquifer" is COUNTERPRODUCTIVE when deep_seep > 0 — the
    # extra percolation is routed to deep storage and lost from streamflow
    # (PBIAS collapsed from -80% to -98% under perco=0.95 + awc cuts).
    # Recommend reducing deep-percolation losses instead; verify perco's true
    # direction with a single-parameter probe before combined moves.
    if lowflow_ratio < 0.9:
        recs.append({
            "param": "flo_min", "direction": "increase", "magnitude": "medium",
            "confidence": 0.7,
            "reason": f"lowflow_ratio={lowflow_ratio:.2f}<0.9, increase flo_min for baseflow",
            "category": "swat"
        })
        recs.append({
            "param": "alpha", "direction": "decrease", "magnitude": "small",
            "confidence": 0.6,
            "reason": ("lowflow too low, decrease alpha to slow aquifer "
                       "recession and sustain dry-season baseflow"),
            "category": "swat"
        })
        recs.append({
            "param": "deep_seep", "direction": "decrease", "magnitude": "small",
            "confidence": 0.5,
            "reason": ("lowflow too low, decrease deep_seep to keep aquifer "
                       "water in the stream (do NOT raise perco: verified to "
                       "route water to deep losses in this project)"),
            "category": "swat"
        })
        recs.append({
            "param": "revap_co", "direction": "decrease", "magnitude": "small",
            "confidence": 0.5,
            "reason": ("lowflow too low, decrease revap_co: less aquifer "
                       "water lost to ET, more baseflow"),
            "category": "swat"
        })
        # Evidence-gated perco (per the perco rule in the prompts): emit a
        # structured perco rec ONLY when the caller supplied current_params
        # and the CURRENT deep_seep is verified very low (<= 0.01). Without
        # that evidence (no current_params, or deep_seep > 0.01) the extra
        # percolation is lost to deep storage, so no perco rec is emitted.
        deep_seep_now = _safe_float(
            (current_params or {}).get("deep_seep"), float("nan"))
        if deep_seep_now <= 0.01:
            recs.append({
                "param": "perco", "direction": "increase", "magnitude": "small",
                "confidence": 0.3,
                "reason": (f"lowflow too low and current deep_seep="
                           f"{deep_seep_now:g} verified <= 0.01, so extra "
                           "percolation reaches the shallow aquifer instead "
                           "of deep losses; run a single-parameter probe "
                           "before any combined move"),
                "category": "swat"
            })

    # Low flow too high → decrease flo_min, increase deep_seep
    if lowflow_ratio > 1.1:
        recs.append({
            "param": "flo_min", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.7,
            "reason": f"lowflow_ratio={lowflow_ratio:.2f}>1.1, decrease flo_min to reduce baseflow",
            "category": "swat"
        })
        recs.append({
            "param": "deep_seep", "direction": "increase", "magnitude": "small",
            "confidence": 0.55,
            "reason": "lowflow too high, increase deep_seep to divert recharge to deep storage",
            "category": "swat"
        })

    # PBIAS too wet (positive = overestimated volume) → decrease cn2, increase esco
    if pbias > 10.0:
        recs.append({
            "param": "cn2", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.75,
            "reason": f"PBIAS={pbias:.1f}% too wet, decrease cn2 (pctchg) to cut surface runoff",
            "category": "swat"
        })
        recs.append({
            "param": "esco", "direction": "increase", "magnitude": "small",
            "confidence": 0.6,
            "reason": "Too wet, increase esco to boost soil evaporation",
            "category": "swat"
        })

    # PBIAS too dry → increase cn2, decrease esco
    if pbias < -10.0:
        recs.append({
            "param": "cn2", "direction": "increase", "magnitude": "medium",
            "confidence": 0.75,
            "reason": f"PBIAS={pbias:.1f}% too dry, increase cn2 (pctchg) to add surface runoff",
            "category": "swat"
        })
        recs.append({
            "param": "esco", "direction": "decrease", "magnitude": "small",
            "confidence": 0.6,
            "reason": "Too dry, decrease esco to reduce soil evaporation",
            "category": "swat"
        })

    # Recession too fast → decrease alpha (slower baseflow recession)
    if recession_k > 1.3:
        recs.append({
            "param": "alpha", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.8,
            "reason": f"recession_k={recession_k:.2f}>1.3, decrease alpha to slow recession",
            "category": "swat"
        })

    # Recession too slow → increase alpha
    if recession_k < 0.7:
        recs.append({
            "param": "alpha", "direction": "increase", "magnitude": "medium",
            "confidence": 0.8,
            "reason": f"recession_k={recession_k:.2f}<0.7, increase alpha to speed recession",
            "category": "swat"
        })

    # Baseflow too low → reduce deep losses. See the empirical note above:
    # raising perco feeds deep_seep losses and DRIES the river in this
    # project; probe perco's direction individually before recommending it.
    if baseflow_bias < -20.0:
        recs.append({
            "param": "deep_seep", "direction": "decrease", "magnitude": "medium",
            "confidence": 0.6,
            "reason": (f"baseflow_bias={baseflow_bias:.1f}% too low, decrease "
                       f"deep_seep so aquifer recharge returns to the stream "
                       f"(perco increase empirically counterproductive here)"),
            "category": "swat"
        })

    # Peak too early → increase surlag (+ lat_ttime), longer surface lag
    if peak_timing < -2.0:
        mag = "large" if peak_timing < -5 else "medium" if peak_timing < -3 else "small"
        recs.append({
            "param": "surlag", "direction": "increase", "magnitude": mag,
            "confidence": min(0.9, 0.5 + abs(peak_timing) * 0.1),
            "reason": f"Peak {peak_timing:.1f}d early, increase surlag to delay peak",
            "category": "swat"
        })
        recs.append({
            "param": "lat_ttime", "direction": "increase", "magnitude": "small",
            "confidence": 0.55,
            "reason": "Peak early, increase lat_ttime to delay subsurface flow",
            "category": "swat"
        })

    # Peak too late → decrease surlag
    if peak_timing > 2.0:
        mag = "large" if peak_timing > 5 else "medium" if peak_timing > 3 else "small"
        recs.append({
            "param": "surlag", "direction": "decrease", "magnitude": mag,
            "confidence": min(0.9, 0.5 + abs(peak_timing) * 0.1),
            "reason": f"Peak {peak_timing:.1f}d late, decrease surlag to advance peak",
            "category": "swat"
        })

    # Peak too sharp but volume ok → increase awc (more soil buffering)
    if peak_mag_bias > 20.0 and abs(pbias) < 10.0:
        recs.append({
            "param": "awc", "direction": "increase", "magnitude": "small",
            "confidence": 0.7,
            "reason": f"Peak mag bias={peak_mag_bias:.1f}% but volume ok, increase awc (pctchg) to buffer peaks",
            "category": "swat"
        })

    # Peak too flat but volume ok → decrease awc
    if peak_mag_bias < -20.0 and abs(pbias) < 10.0:
        recs.append({
            "param": "awc", "direction": "decrease", "magnitude": "small",
            "confidence": 0.7,
            "reason": f"Peak mag bias={peak_mag_bias:.1f}% but volume ok, decrease awc (pctchg) to sharpen peaks",
            "category": "swat"
        })

    return recs


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
