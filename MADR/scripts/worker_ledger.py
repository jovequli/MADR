"""Persistent per-station JSONL records of parameters, scores, diagnostics and reasoning.

Supports calibration-history lookup, warm starts and stagnation queries."""

import json
import os
import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class LedgerEntry:
    """One calibration-round record."""
    round_id: int
    subbasin_id: str
    timestamp: str
    phase: str
    params: Dict[str, float]
    metrics: Dict[str, Any]
    diagnosis_summary: str
    reasoning: str
    selected_action: str
    leader_guidance: str
    nse: float
    nse_delta: float          # Current minus previous NSE
    improved: bool            # Whether the record improves on the ledger best
    stagnation_count: int     # Consecutive rounds without improvement
    escape_label: str = ""    # Escape label: empty=agent-led; T1_3/5=tier-1 round 3 of 5; BEST=fallback
    status: str = "success"
    error: Optional[str] = None


# ============================================================================
# WorkerLedger
# ============================================================================

class WorkerLedger:
    """Append-only station ledger with historical-best and latest-record queries.

    Records support stagnation checks and Leader summaries without an external
    database. Historical-best queries do not replace the executor's accepted state."""

    def __init__(self, output_dir: str, subbasin_id: str):
        self.output_dir = output_dir
        self.subbasin_id = subbasin_id
        self._path = os.path.join(output_dir, f"{subbasin_id}_ledger.jsonl")
        self._stagnation_count = 0
        self._best_nse = float("-inf")

        # Restore state from existing records
        self._restore_state()

    @property
    def path(self) -> str:
        return self._path

    @property
    def exists(self) -> bool:
        return os.path.exists(self._path) and os.path.getsize(self._path) > 0

    @property
    def stagnation_count(self) -> int:
        return self._stagnation_count

    @property
    def best_nse(self) -> float:
        return self._best_nse

    # Write records

    def append(
        self,
        round_id: int,
        phase: str,
        params: Dict[str, float],
        metrics: Dict[str, Any],
        diagnosis_summary: str = "",
        reasoning: str = "",
        selected_action: str = "",
        leader_guidance: str = "",
        escape_label: str = "",
        status: str = "success",
        error: Optional[str] = None,
    ) -> LedgerEntry:
        """Append a round and update NSE change, improvement and stagnation counters."""
        nse = _safe_float(metrics.get("NSE"), float("-inf"))
        valid = status == "success" and math.isfinite(nse)
        if not valid:
            status = "failed"
            error = error or "nonfinite_or_failed_evaluation"
        prev_nse = self._best_nse

        # Check improvement
        improved = valid and nse > prev_nse + 0.001  # Small tolerance avoids floating-point noise

        # Update stagnation count
        if improved:
            self._stagnation_count = 0
            self._best_nse = nse
        elif valid:
            self._stagnation_count += 1

        nse_delta = nse - prev_nse if prev_nse > float("-inf") else 0.0

        entry = LedgerEntry(
            round_id=round_id,
            subbasin_id=self.subbasin_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            phase=phase,
            params=dict(params),
            metrics=_sanitize_metrics(metrics),
            diagnosis_summary=diagnosis_summary,
            reasoning=reasoning,
            selected_action=selected_action,
            leader_guidance=leader_guidance,
            nse=_sanitize_float(nse),
            nse_delta=_sanitize_float(nse_delta),
            improved=improved,
            stagnation_count=self._stagnation_count,
            escape_label=escape_label,
            status=status,
            error=error,
        )

        # Append the record
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "a") as f:
            f.write(json.dumps(asdict(entry), default=str, ensure_ascii=False) + "\n")

        logger.debug(
            f"Ledger append: {self.subbasin_id} round={round_id} "
            f"NSE={nse:.4f} improved={improved} stagnation={self._stagnation_count}"
        )
        return entry

    # Read records

    def read_all(self) -> List[Dict[str, Any]]:
        """Read all records in chronological order, oldest first."""
        if not self.exists:
            return []
        entries = []
        with open(self._path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning(f"Ledger parse error at line {line_num}: {e}")
        return entries

    def count(self) -> int:
        """Return the number of records."""
        if not self.exists:
            return 0
        with open(self._path) as f:
            return sum(1 for line in f if line.strip())

    # ── M1: Revert-to-Best ─────────────────────────────────────────

    def get_best_params(self) -> Optional[Dict[str, Any]]:
        """Return the parameter record with the highest observed NSE.

        The result contains params, nse, round_id and phase, or is None. This
        historical record is distinct from the current accepted joint state."""
        entries = self.read_all()
        if not entries:
            return None

        best = None
        best_nse = float("-inf")

        for entry in entries:
            if entry.get("status", "success") != "success":
                continue
            nse = _safe_float(entry.get("nse"), float("-inf"))
            if nse > best_nse:
                best_nse = nse
                best = entry

        if best is None:
            return None

        return {
            "params": best.get("params", {}),
            "nse": best_nse,
            "round_id": best.get("round_id"),
            "phase": best.get("phase"),
            "metrics": best.get("metrics", {}),
        }

    # Latest-record query

    def get_last_entry(self) -> Optional[Dict[str, Any]]:
        """Return the most recent record."""
        entries = self.read_all()
        return entries[-1] if entries else None

    # Leader summary

    def summary(self) -> Dict[str, Any]:
        """Summarize count, best/current NSE, stagnation, improvement rate and phases."""
        entries = self.read_all()
        if not entries:
            return {
                "subbasin_id": self.subbasin_id,
                "n_entries": 0,
                "best_nse": None,
                "current_nse": None,
                "stagnation_count": 0,
            }

        nse_values = []
        phases = []
        improved_count = 0
        for e in entries:
            nse = _safe_float(e.get("nse"), None)
            if nse is not None:
                nse_values.append(nse)
            phases.append(e.get("phase", "unknown"))
            if e.get("improved"):
                improved_count += 1

        return {
            "subbasin_id": self.subbasin_id,
            "n_entries": len(entries),
            "best_nse": max(nse_values) if nse_values else None,
            "current_nse": nse_values[-1] if nse_values else None,
            "stagnation_count": self._stagnation_count,
            "improvement_rate": round(improved_count / len(entries), 2) if entries else 0,
            "latest_phase": phases[-1] if phases else None,
        }

    # Internal helpers

    def _restore_state(self):
        """Restore stagnation and best-NSE state from saved records."""
        if not self.exists:
            return

        entries = self.read_all()
        if not entries:
            return

        # Restore best NSE
        for entry in entries:
            if entry.get("status", "success") != "success":
                continue
            nse = _safe_float(entry.get("nse"), float("-inf"))
            if nse > self._best_nse:
                self._best_nse = nse

        # Restore stagnation from the latest record
        last = entries[-1]
        self._stagnation_count = last.get("stagnation_count", 0)

        logger.debug(
            f"Ledger restored: {self.subbasin_id} "
            f"entries={len(entries)} best_nse={self._best_nse:.4f} "
            f"stagnation={self._stagnation_count}"
        )


# ============================================================================
# Helper functions
# ============================================================================

def _safe_float(v, default=0.0):
    """Safely extract a finite float."""
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _sanitize_float(v) -> Any:
    """Convert infinite and NaN values to strings for JSON serialization."""
    if isinstance(v, float):
        if math.isinf(v) or math.isnan(v):
            return str(v)
    return v


def _sanitize_metrics(metrics: Dict) -> Dict:
    """Make non-JSON metric values serializable."""
    clean = {}
    for k, v in metrics.items():
        if isinstance(v, float):
            clean[k] = _sanitize_float(v)
        elif isinstance(v, dict):
            clean[k] = _sanitize_metrics(v)
        else:
            clean[k] = v
    return clean
