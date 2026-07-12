"""
Leader Cross-Basin Coordinator — lightweight cross-basin pattern detection.

Pure logic module, no direct LLM calls. Responsible for:
1. Aggregating Worker summaries
2. Detecting cross-basin patterns (upstream cascade, parameter clustering)
3. Generating cross-basin insights for the Leader LLM

Topology (Huaihe Basin):
    Layer 1 (headwater): xixian, zhoukou, jiangjiaji
    Layer 2: wangjiaba = f(xixian, zhoukou)
    Layer 3 (outlet): bengbu = f(wangjiaba, jiangjiaji)

Usage:
    from leader_coordinator import LeaderCoordinator

    coordinator = LeaderCoordinator(upstream_map)
    insights = coordinator.compute_cross_basin_insights(worker_summaries)
    compact_prompt = coordinator.format_for_leader(worker_summaries, insights)
"""

import logging
import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class CrossBasinInsight:
    """A single cross-basin insight."""
    insight_type: str       # "upstream_cascade"|"parameter_cluster"|"routing_mismatch"|"stagnation_cluster"
    severity: str           # "low"|"medium"|"high"
    affected_basins: List[str]
    description: str        # 1-2 sentence description
    recommendation: str     # recommendation for Leader

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CoordinationResult:
    """Full output of the coordinator."""
    insights: List[CrossBasinInsight]
    compact_summary: str        # compact text, embeddable in Leader prompt
    global_status: str          # "healthy"|"attention"|"critical"
    mean_nse: Optional[float]
    outlet_nse: Optional[float]
    n_stagnant: int


# ============================================================================
# LeaderCoordinator
# ============================================================================

class LeaderCoordinator:
    """
    Cross-basin coordinator.

    Detects cross-basin patterns from Worker summaries and generates
    compact coordination information for the Leader LLM.
    """

    def __init__(
        self,
        upstream_map: Dict[str, List[str]],
        outlet_id: str = "bengbu",
        model_combination: str = "vic_routing",
    ):
        self.upstream_map = upstream_map
        self.outlet_id = outlet_id
        self.model_combination = model_combination
        self.downstream_map: Dict[str, List[str]] = self._build_downstream_map()

    # ── Main entry ─────────────────────────────────────────────────

    def compute_cross_basin_insights(
        self,
        worker_summaries: List[Dict[str, Any]],
    ) -> CoordinationResult:
        """Analyze Worker summaries to detect cross-basin patterns."""
        try:
            by_id = {s["subbasin_id"]: s for s in worker_summaries if "subbasin_id" in s}

            insights = []
            insights.extend(self._detect_upstream_cascade(by_id))
            insights.extend(self._detect_parameter_clusters(by_id))
            insights.extend(self._detect_routing_mismatch(by_id))
            insights.extend(self._detect_stagnation_cluster(by_id))

            nse_values = [s.get("current_nse") for s in worker_summaries
                          if s.get("current_nse") is not None and s.get("current_nse") > -999]
            mean_nse = sum(nse_values) / len(nse_values) if nse_values else None
            outlet_nse = by_id.get(self.outlet_id, {}).get("current_nse")

            n_stagnant = sum(1 for s in worker_summaries if s.get("stagnation_risk"))

            if n_stagnant >= len(worker_summaries) // 2:
                global_status = "critical"
            elif any(i.severity == "high" for i in insights):
                global_status = "attention"
            else:
                global_status = "healthy"

            # Generate compact summary
            compact = self._format_compact_summary(
                worker_summaries, insights, mean_nse, outlet_nse
            )

            return CoordinationResult(
                insights=insights,
                compact_summary=compact,
                global_status=global_status,
                mean_nse=round(mean_nse, 4) if mean_nse else None,
                outlet_nse=round(outlet_nse, 4) if outlet_nse else None,
                n_stagnant=n_stagnant,
            )

        except Exception as e:
            logger.error(f"LeaderCoordinator error: {e}")
            return CoordinationResult(
                insights=[],
                compact_summary=f"Coordinator error: {e}",
                global_status="attention",
                mean_nse=None,
                outlet_nse=None,
                n_stagnant=0,
            )

    # ── Pattern detection ────────────────────────────────────────

    def _detect_upstream_cascade(
        self,
        by_id: Dict[str, Dict],
    ) -> List[CrossBasinInsight]:
        """Detect upstream cascade effects."""
        insights = []

        for basin_id, ups in self.upstream_map.items():
            if not ups:
                continue  # headwater

            downstream = by_id.get(basin_id, {})
            ds_issue = downstream.get("primary_issue", "none")
            ds_nse = downstream.get("current_nse", 0)

            for up_id in ups:
                upstream = by_id.get(up_id, {})
                us_issue = upstream.get("primary_issue", "none")
                us_nse = upstream.get("current_nse", 0)

                # Upstream volume issue + downstream timing issue
                if us_issue == "volume" and ds_issue == "timing":
                    volume_params = (
                        "cn2/esco" if self.model_combination == "swat"
                        else "binfilt/soil_d2"
                    )
                    insights.append(CrossBasinInsight(
                        insight_type="upstream_cascade",
                        severity="high",
                        affected_basins=[up_id, basin_id],
                        description=(
                            f"{up_id} has volume bias causing timing issues in {basin_id}. "
                            f"Fix {up_id} runoff generation first."
                        ),
                        recommendation=f"Focus on {up_id} {volume_params} adjustment",
                    ))

                # Upstream NSE poor (< 0.4) and downstream also poor
                if us_nse is not None and ds_nse is not None:
                    if us_nse < 0.4 and ds_nse < 0.5:
                        insights.append(CrossBasinInsight(
                            insight_type="upstream_cascade",
                            severity="medium",
                            affected_basins=[up_id, basin_id],
                            description=(
                                f"{up_id}(NSE={us_nse:.2f}) poor, "
                                f"affecting downstream {basin_id}(NSE={ds_nse:.2f})."
                            ),
                            recommendation=f"Improve upstream {up_id} simulation first",
                        ))

        return insights

    def _detect_parameter_clusters(
        self,
        by_id: Dict[str, Dict],
    ) -> List[CrossBasinInsight]:
        """Detect parameter direction clustering among headwaters."""
        insights = []

        headwaters = [sid for sid, ups in self.upstream_map.items() if not ups]
        if len(headwaters) < 2:
            return insights

        direction_count: Dict[str, Dict[str, int]] = {}
        for hw_id in headwaters:
            summary = by_id.get(hw_id, {})
            for rec in summary.get("top_recommendations", []):
                rec_str = str(rec).lower()
                for param in ["binfilt", "soil_d2", "soil_d3", "Ds", "velocity"]:
                    if param.lower() in rec_str:
                        if param not in direction_count:
                            direction_count[param] = {"increase": 0, "decrease": 0}
                        if "increase" in rec_str:
                            direction_count[param]["increase"] += 1
                        elif "decrease" in rec_str:
                            direction_count[param]["decrease"] += 1

        for param, dirs in direction_count.items():
            for direction, count in dirs.items():
                if count >= 2:
                    hw_names = ", ".join(headwaters)
                    insights.append(CrossBasinInsight(
                        insight_type="parameter_cluster",
                        severity="low",
                        affected_basins=headwaters,
                        description=(
                            f"Multiple headwaters ({hw_names}) suggest {direction} {param}, "
                            f"possible regional pattern."
                        ),
                        recommendation=f"Consider uniform {direction} {param} for headwaters",
                    ))

        return insights

    def _detect_routing_mismatch(
        self,
        by_id: Dict[str, Dict],
    ) -> List[CrossBasinInsight]:
        """Detect routing mismatch: upstream good but downstream poor with high routing responsibility."""
        insights = []

        # SWAT+ has no separate routing module (responsibility is NaN, no split).
        if self.model_combination == "swat":
            return insights

        for basin_id, ups in self.upstream_map.items():
            if not ups:
                continue

            downstream = by_id.get(basin_id, {})
            ds_nse = downstream.get("current_nse")
            ds_routing_resp = downstream.get("routing_responsibility", 0)

            if ds_nse is None:
                continue

            us_nses = []
            for up_id in ups:
                us_nse = by_id.get(up_id, {}).get("current_nse")
                if us_nse is not None and us_nse > -999:
                    us_nses.append(us_nse)

            if not us_nses:
                continue

            avg_us_nse = sum(us_nses) / len(us_nses)

            if avg_us_nse > 0.6 and ds_nse < 0.5 and ds_routing_resp > 0.5:
                if self.model_combination == "vic_cama_flood":
                    routing_label = "CaMa"
                    routing_params = "Hc/Hmin/Wc/Wmin channel geometry"
                else:
                    routing_label = "routing"
                    routing_params = "velocity/diffusivity"
                insights.append(CrossBasinInsight(
                    insight_type="routing_mismatch",
                    severity="high",
                    affected_basins=[basin_id] + ups,
                    description=(
                        f"Upstream mean NSE={avg_us_nse:.2f} good, but {basin_id}(NSE={ds_nse:.2f}) poor, "
                        f"{routing_label} resp.={ds_routing_resp:.0%}. Likely {routing_label} parameter issue."
                    ),
                    recommendation=f"Focus {basin_id} on {routing_params} adjustment",
                ))

        return insights

    def _detect_stagnation_cluster(
        self,
        by_id: Dict[str, Dict],
    ) -> List[CrossBasinInsight]:
        """Detect stagnation clustering across multiple basins."""
        insights = []

        stagnant_basins = [
            sid for sid, s in by_id.items()
            if s.get("stagnation_risk") and s.get("stagnation_count", 0) >= 3
        ]

        if len(stagnant_basins) >= 2:
            names = ", ".join(stagnant_basins)
            counts = [by_id[sid].get("stagnation_count", 0) for sid in stagnant_basins]
            max_count = max(counts) if counts else 0
            insights.append(CrossBasinInsight(
                insight_type="stagnation_cluster",
                severity="high" if len(stagnant_basins) >= 3 else "medium",
                affected_basins=stagnant_basins,
                description=(
                    f"{len(stagnant_basins)} basins ({names}) stagnant simultaneously, "
                    f"max {max_count} rounds. Global strategy adjustment may be needed."
                ),
                recommendation="Consider phase transition or expanding search radius",
            ))

        return insights

    # ── Format output ────────────────────────────────────────────

    def _format_compact_summary(
        self,
        summaries: List[Dict[str, Any]],
        insights: List[CrossBasinInsight],
        mean_nse: Optional[float],
        outlet_nse: Optional[float],
    ) -> str:
        """Generate compact text summary (~300-500 tokens) for Leader prompt."""
        lines = []

        lines.append("=== Global Status ===")
        if mean_nse is not None:
            lines.append(f"Mean NSE: {mean_nse:.4f}")
        if outlet_nse is not None:
            lines.append(f"Outlet NSE: {outlet_nse:.4f}")

        lines.append("\n=== Per-Basin Status ===")
        for s in summaries:
            sid = s.get("subbasin_id", "?")
            nse = s.get("current_nse")
            nse_str = f"{nse:.4f}" if nse is not None and nse > -999 else "N/A"
            issue = s.get("primary_issue", "none")
            trend = s.get("improvement_trend", "?")
            stag = s.get("stagnation_count", 0)
            stag_flag = f" [stagnant {stag} rounds]" if stag >= 3 else ""
            lines.append(f"  {sid}: NSE={nse_str}, issue={issue}, trend={trend}{stag_flag}")

        if insights:
            lines.append("\n=== Cross-Basin Insights ===")
            for i, insight in enumerate(insights[:5], 1):
                lines.append(f"  {i}. [{insight.severity}] {insight.description}")
                lines.append(f"     Recommendation: {insight.recommendation}")

        return "\n".join(lines)

    def format_diagnostics_for_leader(
        self,
        diagnostic_reports: List[Dict[str, Any]],
        coordination: CoordinationResult,
        j_history: List[float],
        nse_history_by_basin: Optional[Dict[str, List[float]]] = None,
    ) -> str:
        """Format diagnostics + cross-basin insights for Leader LLM input (~600-1000 tokens)."""
        lines = []

        # Part 1: Global Status
        lines.append("## Global Status")
        if coordination.mean_nse is not None:
            lines.append(f"Mean NSE: {coordination.mean_nse:.4f}, Outlet NSE: {coordination.outlet_nse}")
        lines.append(f"Global trend: {coordination.global_status}")
        if j_history:
            recent = j_history[-5:]
            trend_str = " -> ".join(f"{v:.4f}" for v in recent)
            lines.append(f"J_total trend (last {len(recent)} rounds): {trend_str}")

        # Part 2: Per-Basin Diagnostics
        lines.append("\n## Per-Basin Diagnostics")
        for report in diagnostic_reports:
            sid = report.get("subbasin_id", "?")
            diag = report.get("diagnosis", {})
            metrics = report.get("current_metrics", {})
            ledger = report.get("ledger_summary", {})

            nse_str = f"{metrics.get('NSE', 'N/A')}"
            if isinstance(metrics.get('NSE'), (int, float)):
                nse_str = f"{metrics['NSE']:.4f}"

            rs = report.get("round_summary", {})

            lines.append(f"\n### {sid}")
            lines.append(
                f"  NSE={nse_str}, PBIAS={metrics.get('PBIAS', 'N/A')}%, "
                f"peak_ratio={metrics.get('peak_ratio', 'N/A')}, "
                f"peak_timing={metrics.get('peak_timing_bias_days', 'N/A')}d, "
                f"recession_k={metrics.get('recession_k_ratio', 'N/A')}, "
                f"baseflow_bias={metrics.get('baseflow_index_bias_pct', 'N/A')}%"
            )
            lines.append(
                f"  summary: Δnse={rs.get('nse_delta', '?')}, "
                f"timing={rs.get('timing_status', '?')}, "
                f"recession={rs.get('recession_status', '?')}, "
                f"stagnant={rs.get('is_stagnant', False)}"
            )
            # Responsibility label per model combination (SWAT+ has no
            # land/routing split — responsibilities are NaN there).
            def _pct(v):
                try:
                    f = float(v)
                    if math.isnan(f) or math.isinf(f):
                        return "N/A"
                    return f"{f:.0%}"
                except (TypeError, ValueError):
                    return "N/A"

            if self.model_combination == "swat":
                resp_str = "model=SWAT+ (integrated land+channel)"
            elif self.model_combination == "vic_cama_flood":
                resp_str = (
                    f"VIC resp.={_pct(diag.get('vic_responsibility', 0))}, "
                    f"CaMa resp.={_pct(diag.get('routing_responsibility', 0))}"
                )
            else:
                resp_str = (
                    f"VIC resp.={_pct(diag.get('vic_responsibility', 0))}, "
                    f"Routing resp.={_pct(diag.get('routing_responsibility', 0))}"
                )
            lines.append(
                f"  Diagnosis: {diag.get('primary_issue', 'none')} issue, "
                f"severity={diag.get('severity', '?')}, "
                f"{resp_str}"
            )
            lines.append(f"  Reasoning: {diag.get('reasoning', 'N/A')}")
            lines.append(
                f"  Trend: {diag.get('improvement_trend', '?')}, "
                f"stagnation={ledger.get('stagnation_count', 0)} rounds, "
                f"best NSE={report.get('best_nse', 'N/A')}"
            )

            if nse_history_by_basin and sid in nse_history_by_basin:
                recent_nse = nse_history_by_basin[sid][-5:]
                if recent_nse:
                    nse_trend = " -> ".join(f"{v:.3f}" for v in recent_nse)
                    lines.append(f"  NSE trajectory: {nse_trend}")

            recs = diag.get("recommendations", [])
            if recs:
                rec_strs = [f"{r['param']} {r['direction']}({r['magnitude']})" for r in recs[:3]]
                lines.append(f"  Diag hints: {'; '.join(rec_strs)}")

        # Part 3: Cross-Basin Insights
        if coordination.insights:
            lines.append("\n## Cross-Basin Insights")
            for i, insight in enumerate(coordination.insights[:5], 1):
                lines.append(f"  {i}. [{insight.severity}] {insight.description}")
                lines.append(f"     Recommendation: {insight.recommendation}")

        # Part 4: Instruction
        lines.append(
            "\n## Provide per-basin parameter adjustment instructions"
            "\nFor each basin specify: which params to adjust (increase/decrease/hold), "
            "magnitude (small/medium/large), and search radius hint (shrink/keep/expand)."
        )

        return "\n".join(lines)

    def build_nse_history_by_basin(
        self,
        round_history: List[Dict[str, Any]],
    ) -> Dict[str, List[float]]:
        """Build per-basin NSE history from LeaderMemory.rounds."""
        result: Dict[str, List[float]] = {}
        for entry in round_history:
            for wr in entry.get("worker_results", []):
                sid = wr.get("subbasin_id", "")
                nse = wr.get("metrics", {}).get("NSE")
                if sid and nse is not None:
                    if sid not in result:
                        result[sid] = []
                    result[sid].append(float(nse))
        return result

    # ── Internal helpers ─────────────────────────────────────────

    def _build_downstream_map(self) -> Dict[str, List[str]]:
        """Build downstream_map from upstream_map."""
        downstream: Dict[str, List[str]] = {sid: [] for sid in self.upstream_map}
        for sid, ups in self.upstream_map.items():
            for up_id in ups:
                if up_id in downstream:
                    downstream[up_id].append(sid)
        return downstream
