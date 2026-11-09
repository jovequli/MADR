"""Conditional cross-station hypotheses from summaries and configured topology.

This module aggregates evidence without making LLM calls.
"""

import logging
import re
from dataclasses import dataclass
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
        outlet_id: Optional[str] = None,
    ):
        self.upstream_map = upstream_map
        sinks = set(upstream_map) - {sid for upstreams in upstream_map.values() for sid in upstreams}
        if outlet_id is None:
            if len(sinks) != 1:
                raise ValueError("Configure outlet_id explicitly when topology has no unique outlet")
            outlet_id = next(iter(sinks))
        elif outlet_id not in sinks:
            raise ValueError("Configured outlet_id must be a known terminal station")
        self.outlet_id = outlet_id

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
                    insights.append(CrossBasinInsight(
                        insight_type="upstream_cascade",
                        severity="high",
                        affected_basins=[up_id, basin_id],
                        description=(
                            f"{up_id} has a volume/high-flow diagnostic issue while {basin_id} has a timing issue. "
                            f"An upstream runoff-generation contribution is a hypothesis to test, not an established cause."
                        ),
                        recommendation=f"Focus on {up_id} binfilt/soil_d2 adjustment",
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
                                f"co-occurs with poor downstream {basin_id}(NSE={ds_nse:.2f}); "
                                f"an upstream contribution is a hypothesis, not demonstrated causation."
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

        # Count independent contributing stations, not duplicate actions from one
        # station. Direction summaries use strings such as "soil_d3 increase".
        # Exact tokens also prevent the legal parameter Dsmax from matching Ds.
        direction_stations: Dict[str, Dict[str, List[str]]] = {}
        pattern = re.compile(r"^\s*(binfilt|soil_d2|soil_d3|Ds|velocity)\s+(increase|decrease)\b", re.I)
        canonical = {p.lower(): p for p in ("binfilt", "soil_d2", "soil_d3", "Ds", "velocity")}
        for hw_id in headwaters:
            summary = by_id.get(hw_id, {})
            for rec in summary.get("top_recommendations", []):
                match = pattern.match(str(rec))
                if not match:
                    continue
                param, direction = canonical[match.group(1).lower()], match.group(2).lower()
                if param not in direction_stations:
                    direction_stations[param] = {"increase": [], "decrease": []}
                contributors = direction_stations[param][direction]
                if hw_id not in contributors:
                    contributors.append(hw_id)

        for param, dirs in direction_stations.items():
            for direction, contributors in dirs.items():
                if len(contributors) >= 2:
                    hw_names = ", ".join(contributors)
                    insights.append(CrossBasinInsight(
                        insight_type="parameter_cluster",
                        severity="low",
                        affected_basins=list(contributors),
                        description=(
                            f"Multiple headwaters ({hw_names}) suggest {direction} {param}, "
                            f"possible regional pattern."
                        ),
                        recommendation=f"Consider uniform {direction} {param} for these contributing headwaters",
                    ))

        return insights

    def _detect_routing_mismatch(
        self,
        by_id: Dict[str, Dict],
    ) -> List[CrossBasinInsight]:
        """Detect routing mismatch: upstream good but downstream poor with high routing responsibility."""
        insights = []

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
                insights.append(CrossBasinInsight(
                    insight_type="routing_mismatch",
                    severity="high",
                    affected_basins=[basin_id] + ups,
                    description=(
                        f"Upstream mean NSE={avg_us_nse:.2f} good, but {basin_id}(NSE={ds_nse:.2f}) poor, "
                        f"routing heuristic priority={ds_routing_resp:.0%}. "
                        f"Testing routing parameters is a hypothesis; these diagnostics do not identify the cause."
                    ),
                    recommendation=f"Focus {basin_id} on velocity/diffusivity adjustment",
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
                recommendation=("Reassess the hypotheses for parameters allowed in the scheduled target phase. "
                                "Phase transitions follow the fixed schedule; search-radius control is inactive. "
                                "At terminal review, summarize only; no next proposal is scheduled."),
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
        """Generate a compact text summary for the Leader prompt."""
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

