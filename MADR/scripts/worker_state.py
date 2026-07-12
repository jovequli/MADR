"""
Worker执行器 — 将Leader的具体指令转化为候选参数集

中央集权架构: Leader做决策，Worker负责执行。
本模块的职责是把Leader给出的结构化指令(哪个参数往哪调多少)
转化为2组具体的候选参数值。

不做诊断(由worker_diagnostics负责上报)。
不做自主决策(决策权在Leader)。
不跳过LLM(是否调LLM由orchestrator控制)。

Usage:
    from worker_state import WorkerExecutor

    executor = WorkerExecutor(param_ranges=ranges)

    candidates = executor.generate_candidates(
        base_params=best_params,          # 搜索中心(历史最优)
        leader_instruction=instruction,    # Leader的具体指令
        phase="vic_only",
    )
    # candidates: [Dict, Dict]  — 恰好2组
"""

import math
import random
import logging
from typing import Dict, List, Optional, Any, Tuple

try:  # 组合感知的阶段可调参数表(权威表在param_validator, 与leader fallback共用)
    from .param_validator import CalibrationPhase, get_tunable_keys
except ImportError:  # 平铺(非包)用法
    from param_validator import CalibrationPhase, get_tunable_keys

logger = logging.getLogger(__name__)


# ============================================================================
# 参数搜索范围(与VIC模型一致)
# ============================================================================

PARAM_BOUNDS = {
    "binfilt":     (0.001, 0.4),
    "Ds":          (0.0001, 1.0),
    "Dsmax":       (1.0, 30.0),
    "Ws":          (0.5, 1.0),
    "soil_d2":     (0.2, 1.5),
    "soil_d3":     (0.5, 2.0),
    "velocity":    (0.5, 3.0),
    "diffusivity": (200.0, 4000.0),
}

# magnitude → 占参数范围的比例
MAGNITUDE_FRACTION = {
    "small":  0.10,
    "medium": 0.22,
    "large":  0.40,
    "none":   0.0,
}

# Legacy常量(保留兼容): 活跃参数集现由param_validator.get_tunable_keys给出
VIC_KEYS = ["binfilt", "Ds", "Dsmax", "Ws", "soil_d2", "soil_d3"]
ROUTING_KEYS = ["velocity", "diffusivity"]


# ============================================================================
# WorkerExecutor
# ============================================================================

class WorkerExecutor:
    """
    Worker参数执行器。

    接收Leader的结构化指令，生成恰好2组候选参数:
      - 候选A: 严格按Leader指令方向调整(保守)
      - 候选B: 在Leader指令方向上更大步长(激进)
    """

    def __init__(
        self,
        param_ranges: Optional[Dict[str, Tuple[float, float]]] = None,
        model_combination: str = "vic_routing",
    ):
        self.ranges = param_ranges or dict(PARAM_BOUNDS)
        # 模型组合(vic_routing/vic_cama_flood/swat), 决定各阶段可调参数集。
        # 默认vic_routing = 旧行为; 调用方也可在构造后直接对该属性赋值。
        self.model_combination = model_combination or "vic_routing"

    def generate_candidates(
        self,
        base_params: Dict[str, float],
        leader_instruction: Dict[str, Any],
        phase: str = "joint",
        radius: Optional[Dict[str, float]] = None,
        escape_mode: bool = False,
        decay_factor: float = 1.0,
        station_stagnation: int = 0,
    ) -> List[Dict[str, float]]:
        """
        将Leader指令转化为1组候选参数（agent主导模式）。

        Parameters
        ----------
        base_params : dict
            搜索中心(通常是历史最优参数, 由ledger提供)
        leader_instruction : dict
            Leader的具体指令
        phase : str
            校准阶段，决定哪些参数可调
        radius : dict, optional
            当前搜索半径(如果提供，用于scaling)
        escape_mode : bool
            全局逃逸标志(保留兼容)
        decay_factor : float
            衰减因子
        station_stagnation : int
            该站连续无改善轮数(仅用于日志)

        Returns
        -------
        List[Dict[str, float]]
            1组候选参数
        """
        try:
            adjustments = leader_instruction.get("param_adjustments", [])
            active_keys = self._get_active_keys(phase)

            # 解析Leader指令为 {param: (direction_sign, fraction)}
            instructions = {}
            for adj in adjustments:
                param = adj.get("param", adj.get("parameter", adj.get("parameter_name", adj.get("param_name", adj.get("name", "")))))
                direction = adj.get("direction", "hold")
                magnitude = adj.get("magnitude", "none")

                if param not in active_keys:
                    continue
                if direction == "hold" or magnitude in ("none", "0", 0):
                    continue

                sign = 1.0 if direction == "increase" else -1.0
                frac = MAGNITUDE_FRACTION.get(str(magnitude).lower(), 0.1)
                instructions[param] = (sign, frac)

            # cali5.2_LW: decay_factor unused (always 1.0), kept for compat
            logger.info("  [AGENT] stagnation=%d", station_stagnation)

            # 单候选: 按指令步长 × 衰减
            candidate = self._apply_instruction(
                base_params, instructions, active_keys,
                intensity=1.0 * decay_factor, radius=radius
            )

            return [candidate]

        except Exception as e:
            logger.error(f"WorkerExecutor.generate_candidates error: {e}")
            return self._fallback_candidates(base_params, phase)

    def generate_escape_candidates(
        self,
        base_params: Dict[str, float],
        phase: str = "joint",
        perturbation_pct: float = 0.10,
        n_params_to_perturb: int = 0,
        phase_scale: float = 1.0,
    ) -> List[Dict[str, float]]:
        """
        逃逸模式候选生成（随机扰动，独立于Leader指令）。

        Parameters
        ----------
        base_params : dict
            escape_best参数（escape内部最优）
        phase : str
            校准阶段
        perturbation_pct : float
            扰动幅度占参数range的比例（如0.10=10%）
        n_params_to_perturb : int
            随机选择扰动的参数个数。0=全部active参数
        phase_scale : float
            阶段缩放因子（VIC=1.0, ROUTING=0.4, JOINT=0.15）

        Returns
        -------
        List[Dict[str, float]]
            1组候选参数
        """
        active_keys = self._get_active_keys(phase)
        scaled_pct = perturbation_pct * phase_scale

        # 选择要扰动的参数
        if n_params_to_perturb > 0 and n_params_to_perturb < len(active_keys):
            perturb_keys = random.sample(active_keys, n_params_to_perturb)
        else:
            perturb_keys = list(active_keys)

        logger.info("  [ESCAPE] pct=%.1f%% (raw=%.0f%% × scale=%.2f), "
                     "perturb %d/%d params: %s",
                     scaled_pct * 100, perturbation_pct * 100, phase_scale,
                     len(perturb_keys), len(active_keys), perturb_keys)

        candidate = dict(base_params)
        for key in active_keys:
            if key not in base_params:
                continue
            lo, hi = self.ranges.get(key, (0, 1))
            if key in perturb_keys:
                r = (hi - lo) * scaled_pct
                new_val = base_params[key] + random.uniform(-r, r)
            else:
                # 未选中的参数: 极微扰动保持稳定
                noise = random.gauss(0, (hi - lo) * 0.005)
                new_val = base_params[key] + noise
            candidate[key] = max(lo, min(hi, new_val))

        return [candidate]

    # ── 内部方法 ───────────────────────────────────────────────────

    def _apply_instruction(
        self,
        base_params: Dict[str, float],
        instructions: Dict[str, Tuple[float, float]],
        active_keys: List[str],
        intensity: float,
        radius: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """按指令生成一组候选。"""
        candidate = dict(base_params)

        for key in active_keys:
            if key not in base_params:
                continue
            lo, hi = self.ranges.get(key, (0, 1))
            param_range = hi - lo

            if key in instructions:
                sign, frac = instructions[key]
                step = sign * param_range * frac * intensity
                # 微小高斯噪声(防止两组完全对称)
                noise = random.gauss(0, param_range * 0.01)
                new_val = base_params[key] + step + noise
            else:
                # Leader未指示的参数: 极微扰动(保持稳定)
                noise = random.gauss(0, param_range * 0.005)
                new_val = base_params[key] + noise

            candidate[key] = max(lo, min(hi, new_val))

        return candidate

    def _get_active_keys(self, phase: str) -> List[str]:
        """按校准阶段过滤活跃参数(组合感知, phase大小写不敏感)。"""
        try:
            phase_enum = CalibrationPhase(str(phase or "").strip().upper())
        except ValueError:
            # 未知阶段: 保持旧行为(视为JOINT, 全参数可调)
            phase_enum = CalibrationPhase.JOINT
        keys = get_tunable_keys(phase_enum, self.model_combination)
        return [k for k in keys if k in self.ranges]

    def _fallback_candidates(
        self,
        base_params: Dict[str, float],
        phase: str,
    ) -> List[Dict[str, float]]:
        """安全降级: 微扰动。"""
        active_keys = self._get_active_keys(phase)
        candidates = []
        for _ in range(2):
            c = dict(base_params)
            for key in active_keys:
                if key not in base_params:
                    continue
                lo, hi = self.ranges.get(key, (0, 1))
                noise = random.gauss(0, (hi - lo) * 0.03)
                c[key] = max(lo, min(hi, base_params[key] + noise))
            candidates.append(c)
        return candidates
