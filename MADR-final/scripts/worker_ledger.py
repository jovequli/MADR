"""
Worker持久化存储 — 每流域JSONL追加式日志

每个子流域一个JSONL文件，记录每轮校准的参数、指标、诊断和推理。
支持warm-start(从历史最优参数出发)和停滞检测。

文件布局: {output_dir}/{subbasin_id}_ledger.jsonl

参考: 同事Structure B的ZoneLedger (core/ledger.py)

Usage:
    from worker_ledger import WorkerLedger

    ledger = WorkerLedger(output_dir="output", subbasin_id="xixian")

    # 每轮结束后追加
    ledger.append(
        round_id=5,
        phase="vic_only",
        params={"binfilt": 0.2, "Ds": 0.05, ...},
        metrics={"NSE": 0.72, "PBIAS": -3.2, ...},
        diagnosis_summary="volume问题, 严重度moderate, VIC责任90%",
        reasoning="减小binfilt降低峰值，NSE从0.68→0.72",
        selected_action="vic_001: 减小binfilt",
        leader_guidance="建议关注峰值匹配",
    )

    # 读取历史最优参数(实现M1 revert-to-best)
    best = ledger.get_best_params()

    # 检测停滞(支撑M3停滞逃逸)
    if ledger.is_stagnant(patience=5):
        # 触发逃逸机制
        ...
"""

import json
import os
import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class LedgerEntry:
    """一轮校准记录。"""
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
    nse_delta: float          # 本轮NSE - 上轮NSE
    improved: bool            # 本轮是否改善
    stagnation_count: int     # 连续无改善计数
    escape_label: str = ""    # 逃逸标签: ""=agent主导, "T1_3/5"=Tier1第3轮, "BEST"=保底
    status: str = "success"
    error: Optional[str] = None


# ============================================================================
# WorkerLedger
# ============================================================================

class WorkerLedger:
    """
    单个子流域的持久化JSONL存储。

    - 追加写入，无需外部数据库
    - 支持历史最优查询(M1 revert-to-best)
    - 支持停滞检测(M3逃逸)
    - 支持最近N轮窗口摘要(给Leader)
    """

    def __init__(self, output_dir: str, subbasin_id: str):
        self.output_dir = output_dir
        self.subbasin_id = subbasin_id
        self._path = os.path.join(output_dir, f"{subbasin_id}_ledger.jsonl")
        self._stagnation_count = 0
        self._best_nse = float("-inf")

        # 从已有文件恢复状态
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

    # ── 写入 ───────────────────────────────────────────────────────

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
        """
        追加一轮校准记录。

        自动计算NSE delta、改善标志和停滞计数。
        """
        nse = _safe_float(metrics.get("NSE"), float("-inf"))
        prev_nse = self._best_nse

        # 判断是否改善
        improved = nse > prev_nse + 0.001  # 微小阈值避免浮动

        # 更新停滞计数
        if improved:
            self._stagnation_count = 0
            self._best_nse = nse
        else:
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

        # 追加写入
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "a") as f:
            f.write(json.dumps(asdict(entry), default=str, ensure_ascii=False) + "\n")

        logger.debug(
            f"Ledger append: {self.subbasin_id} round={round_id} "
            f"NSE={nse:.4f} improved={improved} stagnation={self._stagnation_count}"
        )
        return entry

    # ── 读取 ───────────────────────────────────────────────────────

    def read_all(self) -> List[Dict[str, Any]]:
        """读取所有记录，按时间顺序(最旧在前)。"""
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
        """记录总数。"""
        if not self.exists:
            return 0
        with open(self._path) as f:
            return sum(1 for line in f if line.strip())

    # ── M1: Revert-to-Best ─────────────────────────────────────────

    def get_best_params(self) -> Optional[Dict[str, Any]]:
        """
        获取历史最优NSE对应的参数集。

        实现M1 revert-to-best: Worker每轮从best_params出发探索，
        而不是从漂移的center出发。

        Returns
        -------
        dict or None
            {"params": {...}, "nse": float, "round_id": int, "phase": str}
        """
        entries = self.read_all()
        if not entries:
            return None

        best = None
        best_nse = float("-inf")

        for entry in entries:
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

    def get_best_nse(self) -> float:
        """获取历史最优NSE值。"""
        return self._best_nse

    # ── M3: 停滞检测 ───────────────────────────────────────────────

    def is_stagnant(self, patience: int = 5) -> bool:
        """
        检测是否局部停滞(连续patience轮未改善)。

        Parameters
        ----------
        patience : int
            容忍的连续无改善轮数
        """
        return self._stagnation_count >= patience

    def get_stagnation_tier(self) -> int:
        """
        获取停滞逃逸级别。

        Tier 0: 未停滞 (stagnation < 3)
        Tier 1: 轻度停滞 (3-5轮) → 诊断引导扰动
        Tier 2: 中度停滞 (6-8轮) → 50%半径随机重启
        Tier 3: 重度停滞 (>8轮) → 100%全随机重启
        """
        if self._stagnation_count < 3:
            return 0
        elif self._stagnation_count <= 5:
            return 1
        elif self._stagnation_count <= 8:
            return 2
        else:
            return 3

    # ── 窗口查询 ───────────────────────────────────────────────────

    def get_recent_window(self, n: int = 5) -> List[Dict[str, Any]]:
        """获取最近N轮记录(最近在后)。"""
        entries = self.read_all()
        return entries[-n:] if len(entries) >= n else entries

    def get_last_entry(self) -> Optional[Dict[str, Any]]:
        """获取最近一条记录。"""
        entries = self.read_all()
        return entries[-1] if entries else None

    def get_last_params(self) -> Optional[Dict[str, float]]:
        """获取最近一轮的参数值。"""
        last = self.get_last_entry()
        return last.get("params") if last else None

    def get_last_metrics(self) -> Optional[Dict[str, Any]]:
        """获取最近一轮的指标。"""
        last = self.get_last_entry()
        return last.get("metrics") if last else None

    # ── 摘要(给Leader) ────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        """
        生成ledger摘要，用于Leader汇总。

        Returns
        -------
        dict
            n_entries, best_nse, current_nse, stagnation_count,
            improvement_rate, phase_history
        """
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

    # ── 内部方法 ───────────────────────────────────────────────────

    def _restore_state(self):
        """从已有文件恢复stagnation_count和best_nse。"""
        if not self.exists:
            return

        entries = self.read_all()
        if not entries:
            return

        # 恢复best_nse
        for entry in entries:
            nse = _safe_float(entry.get("nse"), float("-inf"))
            if nse > self._best_nse:
                self._best_nse = nse

        # 恢复stagnation_count(从最后一条记录)
        last = entries[-1]
        self._stagnation_count = last.get("stagnation_count", 0)

        logger.debug(
            f"Ledger restored: {self.subbasin_id} "
            f"entries={len(entries)} best_nse={self._best_nse:.4f} "
            f"stagnation={self._stagnation_count}"
        )


# ============================================================================
# 辅助函数
# ============================================================================

def _safe_float(v, default=0.0):
    """安全提取浮点值。"""
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
    """将inf/nan转为字符串以便JSON序列化。"""
    if isinstance(v, float):
        if math.isinf(v) or math.isnan(v):
            return str(v)
    return v


def _sanitize_metrics(metrics: Dict) -> Dict:
    """清理指标字典中的非JSON值。"""
    clean = {}
    for k, v in metrics.items():
        if isinstance(v, float):
            clean[k] = _sanitize_float(v)
        elif isinstance(v, dict):
            clean[k] = _sanitize_metrics(v)
        else:
            clean[k] = v
    return clean
