"""热电偶校准记录：修正值插值、适用温区与测量不确定度。

计量口径
~~~~~~~~
* 校准点给出“指示温度 → 修正值”的对应关系，修正值**加在指示读数上**
  （corrected = indicated + correction），点与点之间线性插值；
  指示温度超出校准点覆盖范围时钳制到最近端点的修正值（不外推）。
* 适用温区 [applicable_low_c, applicable_high_c] 是该校准记录的有效范围：
  落在区外的读数不做修正（保留原始值），由引擎逐条定位并判
  CAL_OUT_OF_RANGE。
* 测量不确定度：记录级 uncertainty_c 为扩展不确定度（如 k=2）；校准点可
  单独给出点级不确定度，缺省回退到记录级，温度方向上同样线性插值。
* 有效期 [calibrated_at, valid_until] 按批次读数时刻判定：批次读数超出
  有效期即视为校准过期（CAL_EXPIRED），修正不施加。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class CalPoint:
    temperature_c: float
    correction_c: float
    uncertainty_c: Optional[float] = None  # 缺省回退记录级


@dataclass
class CalibrationRecord:
    sensor_id: str
    version: str
    points: list[CalPoint]  # 按 temperature_c 严格递增
    applicable_low_c: float
    applicable_high_c: float
    uncertainty_c: float  # 记录级扩展不确定度 (degC)
    calibrated_at: datetime
    valid_until: datetime
    calibration_id: Optional[int] = None
    note: Optional[str] = None

    def _bracket_idx(self, temp_c: float) -> tuple[int, int]:
        """temp_c 所在的校准点区间下标；点集外钳制到端点。"""
        pts = self.points
        if len(pts) == 1 or temp_c <= pts[0].temperature_c:
            return 0, 0
        if temp_c >= pts[-1].temperature_c:
            return len(pts) - 1, len(pts) - 1
        for i in range(1, len(pts)):
            if temp_c <= pts[i].temperature_c:
                return i - 1, i
        return len(pts) - 1, len(pts) - 1

    def _interp(self, temp_c: float, attr: str) -> float:
        i, j = self._bracket_idx(temp_c)
        pi, pj = self.points[i], self.points[j]
        vi, vj = getattr(pi, attr), getattr(pj, attr)
        if attr == "uncertainty_c":
            vi = self.uncertainty_c if vi is None else vi
            vj = self.uncertainty_c if vj is None else vj
        if i == j:
            return vi
        f = (temp_c - pi.temperature_c) / (pj.temperature_c - pi.temperature_c)
        return vi + f * (vj - vi)

    def correction_at(self, temp_c: float) -> float:
        """指示温度 temp_c 处的修正值 (degC)，加在读数上。"""
        return self._interp(temp_c, "correction_c")

    def uncertainty_at(self, temp_c: float) -> float:
        """temp_c 处的测量不确定度 (degC)；点级缺省回退记录级。"""
        return self._interp(temp_c, "uncertainty_c")

    def bracket(self, temp_c: float) -> list[CalPoint]:
        """temp_c 插值实际使用的校准点（1~2 个）——即“导致变化的校准点”。"""
        i, j = self._bracket_idx(temp_c)
        if i == j:
            return [self.points[i]]
        return [self.points[i], self.points[j]]

    def covers(self, temp_c: float) -> bool:
        return self.applicable_low_c <= temp_c <= self.applicable_high_c


def record_from_view(v) -> CalibrationRecord:
    """从 API 视图（pydantic 模型或 dict）构造校准记录。"""
    get = (lambda k: v[k]) if isinstance(v, dict) else (lambda k: getattr(v, k))
    points = []
    for p in get("points"):
        if isinstance(p, dict):
            points.append(CalPoint(p["temperature_c"], p["correction_c"],
                                   p.get("uncertainty_c")))
        else:
            points.append(CalPoint(p.temperature_c, p.correction_c, p.uncertainty_c))
    return CalibrationRecord(
        sensor_id=get("sensor_id"), version=get("version"), points=points,
        applicable_low_c=get("applicable_low_c"),
        applicable_high_c=get("applicable_high_c"),
        uncertainty_c=get("uncertainty_c"),
        calibrated_at=get("calibrated_at"), valid_until=get("valid_until"),
    )
