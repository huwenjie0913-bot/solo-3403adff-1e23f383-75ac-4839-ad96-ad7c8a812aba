"""请求/响应 Pydantic 模型。

所有时间戳统一使用 ISO 8601；温度输入支持 c/f/k 三种量纲，
入引擎后一律换算为摄氏度 (degC)，速率单位为 degC/min。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------- 枚举 ----------

class TemperatureUnit(str, Enum):
    C = "c"
    F = "f"
    K = "k"


class StageKind(str, Enum):
    HEAT = "heat"       # 升温
    SOAK = "soak"       # 保温
    COOL = "cool"       # 降温


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# ---------- 请求 ----------

class ZoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zone_id: str = Field(..., min_length=1, description="炉区编号")
    sensor_ids: list[str] = Field(..., min_length=1, description="该炉区下的热电偶编号")
    max_uniformity_delta: Optional[float] = Field(
        None, ge=0, description="本炉区允许的最大区间温差 (degC)，缺省取 2 × 传感器容差"
    )


class StageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage_id: str = Field(..., min_length=1, description="材料阶段编号")
    name: Optional[str] = Field(None, description="阶段名称")
    kind: StageKind = Field(..., description="阶段类型：heat/soak/cool")
    start: datetime = Field(..., description="阶段开始时刻")
    end: datetime = Field(..., description="阶段结束时刻")
    target_low_c: Optional[float] = Field(None, description="目标保温带下限 (degC)")
    target_high_c: Optional[float] = Field(None, description="目标保温带上限 (degC)")
    max_heat_rate_c_min: Optional[float] = Field(None, ge=0, description="本阶段升温速率上限 (degC/min)")
    max_cool_rate_c_min: Optional[float] = Field(None, ge=0, description="本阶段降温速率上限 (degC/min，取绝对值)")
    min_soak_seconds: Optional[float] = Field(None, ge=0, description="要求的最短保温时长 (s)，仅 soak 有效")

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: datetime, info) -> datetime:
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("end 必须晚于 start")
        return v


class ReadingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    t: datetime
    sensor_id: str = Field(..., min_length=1)
    value: float = Field(..., description="温度数值；NaN/Inf 不允许")
    unit: TemperatureUnit = TemperatureUnit.C

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("温度必须为有限数值")
        return v


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_no: str = Field(..., min_length=1, description="批次号（检索主键之一）")
    furnace_id: Optional[str] = Field(None, description="炉号")
    recipe_id: str = Field(..., min_length=1, description="配方编号")
    recipe_version: str = Field(..., min_length=1, description="配方版次")
    rule_version: Optional[str] = Field(
        None, description="规则版本，缺省取当前最新版本"
    )
    calibration_version: Optional[str] = Field(
        None, description="校准版本；给定时按该版本的校准记录逐点修正读数并做不确定度评估"
    )
    zones: list[ZoneIn] = Field(..., min_length=1)
    stages: list[StageIn] = Field(..., min_length=1)
    readings: list[ReadingIn] = Field(..., min_length=1)
    # 全局规则参数
    max_heat_rate_c_min: Optional[float] = Field(None, ge=0)
    max_cool_rate_c_min: Optional[float] = Field(None, ge=0)
    max_overshoot_c: float = Field(..., ge=0, description="允许超调 (degC)，加在保温带上限之上")
    sensor_tolerance_c: float = Field(..., ge=0, description="传感器容差 (degC)")
    nominal_sample_seconds: Optional[float] = Field(
        None, gt=0, description="标称采样间隔 (s)，缺省取全批次采样间隔中位数"
    )
    max_sample_jitter: float = Field(
        1.5, gt=1, description="采样间隔相对标称值的允许倍数，超过判 SAMPLE_GAP"
    )
    offline_gap_factor: float = Field(
        3.0, gt=1, description="相邻读数间隔超过该倍数标称间隔即视为测点掉线"
    )
    offline_error_factor: float = Field(
        6.0, gt=1, description="掉线超过该倍数判 error（SENSOR_OFFLINE），否则 warning（SAMPLE_GAP）"
    )


# ---------- 响应 ----------

class CitedReading(BaseModel):
    t: datetime
    sensor_id: str
    value_c: float = Field(..., description="原始读数换算的摄氏度（修正前）")
    unit_in: TemperatureUnit
    raw_value: float
    role: str = Field(..., description="该读数在本判定中的作用，如 before/after/peak/gap_left/gap_right")
    corrected_c: Optional[float] = Field(None, description="施加校准修正后的摄氏度；未修正为 null")


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str
    stage_id: Optional[str] = None
    zone_id: Optional[str] = None
    sensor_id: Optional[str] = None
    first_at: Optional[datetime] = Field(None, description="第一处越界时刻")
    metric: Optional[str] = None
    limit_value: Optional[float] = None
    observed: Optional[float] = None
    unit: Optional[str] = None
    pending: bool = Field(False, description="阈值落入测量不确定区间，结论待定")
    uncertainty_c: Optional[float] = Field(None, description="该判定引用的测量不确定度 (degC 或换算后)")
    citations: list[CitedReading] = Field(default_factory=list)


class SensorStageResult(BaseModel):
    sensor_id: str
    max_rate_c_min: Optional[float] = None
    min_rate_c_min: Optional[float] = None
    rate_pairs: int = 0
    entered_band_at: Optional[datetime] = None
    left_band_at: Optional[datetime] = None
    in_band_seconds: float = 0.0
    longest_in_band_seconds: float = 0.0
    overtemp_seconds: float = 0.0
    max_temperature_c: Optional[float] = None
    corrected: bool = Field(False, description="本阶段读数是否施加了校准修正")
    uncertainty_c: Optional[float] = Field(
        None, description="本阶段读数的最大测量不确定度 (degC)，即误差带 ±"
    )
    notes: list[str] = Field(default_factory=list)


class ZoneStageResult(BaseModel):
    zone_id: str
    sensor_ids: list[str]
    max_spread_c: Optional[float] = None
    uniform_seconds: float = 0.0
    nonuniform_seconds: float = 0.0
    overtemp_seconds: float = 0.0
    simultaneous_in_band_seconds: float = 0.0
    longest_simultaneous_in_band_seconds: float = 0.0
    notes: list[str] = Field(default_factory=list)
    sensors: list[SensorStageResult] = Field(default_factory=list)


class StageResult(BaseModel):
    stage_id: str
    name: Optional[str]
    kind: StageKind
    start: datetime
    end: datetime
    target_low_c: Optional[float]
    target_high_c: Optional[float]
    zones: list[ZoneStageResult] = Field(default_factory=list)


# ---------- 判定差异（并列查看 / 校准影响共用） ----------

class FindingDiff(BaseModel):
    code: str
    severity: Severity
    stage_id: Optional[str]
    zone_id: Optional[str]
    sensor_id: Optional[str]
    first_at_a: Optional[datetime]
    first_at_b: Optional[datetime]
    observed_a: Optional[float]
    observed_b: Optional[float]
    limit_value: Optional[float]
    metric: Optional[str]
    unit: Optional[str]
    message_a: Optional[str]
    message_b: Optional[str]


# ---------- 校准修正影响 ----------

class CalibrationPointRef(BaseModel):
    sensor_id: str
    temperature_c: float = Field(..., description="校准点温度 (degC)")
    correction_c: float = Field(..., description="该点修正值 (degC)")
    uncertainty_c: float = Field(..., description="该点有效不确定度 (degC)")


class FindingCalibrationLink(BaseModel):
    code: str
    stage_id: Optional[str] = None
    zone_id: Optional[str] = None
    sensor_id: Optional[str] = None
    change: str = Field(..., description="added=修正后新增 / removed=修正后消除 / changed=数值或严重度变化")
    reading_t: Optional[datetime] = Field(None, description="判定涉及的相关读数时刻")
    points: list[CalibrationPointRef] = Field(
        default_factory=list, description="导致该变化的校准点（插值实际使用的点）"
    )


class CalibrationImpact(BaseModel):
    version: str
    status_before: str = Field(..., description="未施加校准修正时的结论")
    status_after: str = Field(..., description="施加校准修正后的结论")
    verdict_changed: bool
    sensors_corrected: list[str] = Field(default_factory=list)
    sensors_missing: list[str] = Field(default_factory=list, description="无该校准版本记录的传感器")
    sensors_expired: list[str] = Field(default_factory=list, description="校准有效期未覆盖批次时刻")
    sensors_out_of_range: list[str] = Field(default_factory=list, description="存在超出适用温区读数")
    sensor_uncertainty_c: dict[str, float] = Field(
        default_factory=dict, description="各已修正传感器的记录级不确定度（误差带 ±）"
    )
    added: list[FindingDiff] = Field(default_factory=list, description="修正后新增的判定")
    removed: list[FindingDiff] = Field(default_factory=list, description="修正后消除的判定")
    changed: list[FindingDiff] = Field(default_factory=list, description="修正后数值/严重度变化的判定")
    responsible_points: list[FindingCalibrationLink] = Field(
        default_factory=list, description="各变化判定对应的校准点"
    )


class EvaluationReport(BaseModel):
    evaluation_id: Optional[int] = None
    batch_no: str
    furnace_id: Optional[str]
    recipe_id: str
    recipe_version: str
    rule_version: str
    calibration_version: Optional[str] = None
    status: Literal["PASS", "FAIL", "INPUT_ERROR", "PENDING"]
    first_violation: Optional[Finding] = None
    findings: list[Finding] = Field(default_factory=list)
    stages: list[StageResult] = Field(default_factory=list)
    calibration_impact: Optional[CalibrationImpact] = None
    input_summary: dict
    evaluated_at: datetime


# ---------- 并列查看 ----------

class CompareResponse(BaseModel):
    batch_no: str
    a: EvaluationReport
    b: Optional[EvaluationReport]
    status_a: str
    status_b: Optional[str]
    same_verdict: bool
    added: list[FindingDiff]
    removed: list[FindingDiff]
    changed: list[FindingDiff]


# ---------- 校准登记与版本对比 ----------

class CalibrationPointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature_c: float = Field(..., description="校准点温度 (degC)")
    correction_c: float = Field(..., description="该点修正值 (degC)，加在指示读数上")
    uncertainty_c: Optional[float] = Field(
        None, ge=0, description="该点测量不确定度 (degC)，缺省取记录级"
    )


class CalibrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sensor_id: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1, description="校准版本号（同一传感器下唯一）")
    points: list[CalibrationPointIn] = Field(..., min_length=1, description="校准点，温度须严格递增")
    applicable_low_c: float = Field(..., description="适用温区下限 (degC)")
    applicable_high_c: float = Field(..., description="适用温区上限 (degC)")
    uncertainty_c: float = Field(..., ge=0, description="记录级扩展测量不确定度 (degC)")
    calibrated_at: datetime = Field(..., description="校准日期")
    valid_until: datetime = Field(..., description="有效期至")
    note: Optional[str] = None

    @field_validator("points")
    @classmethod
    def _points_sorted(cls, v: list[CalibrationPointIn]) -> list[CalibrationPointIn]:
        temps = [p.temperature_c for p in v]
        if any(b <= a for a, b in zip(temps, temps[1:])):
            raise ValueError("校准点温度必须严格递增")
        return v

    @model_validator(mode="after")
    def _range_and_validity(self):
        if self.applicable_high_c <= self.applicable_low_c:
            raise ValueError("applicable_high_c 必须大于 applicable_low_c")
        if self.valid_until <= self.calibrated_at:
            raise ValueError("valid_until 必须晚于 calibrated_at")
        for p in self.points:
            if not (self.applicable_low_c <= p.temperature_c <= self.applicable_high_c):
                raise ValueError(f"校准点 {p.temperature_c}°C 超出适用温区")
        return self


class CalibrationRecordView(CalibrationIn):
    calibration_id: int
    created_at: datetime


class CalibrationPointDiff(BaseModel):
    temperature_c: float
    correction_a: Optional[float]
    correction_b: Optional[float]
    uncertainty_a: Optional[float]
    uncertainty_b: Optional[float]


class CalibrationCompareResponse(BaseModel):
    sensor_a: str
    sensor_b: str
    version_a: str
    version_b: str
    same_sensor: bool
    points_added: list[CalibrationPointDiff] = Field(description="B 有而 A 无的校准点")
    points_removed: list[CalibrationPointDiff] = Field(description="A 有而 B 无的校准点")
    points_changed: list[CalibrationPointDiff] = Field(description="同温度点但修正值/不确定度变化")
    uncertainty_a: float
    uncertainty_b: float
    applicable_range_a: list[float]
    applicable_range_b: list[float]
    valid_until_a: datetime
    valid_until_b: datetime
    overlap_range_c: Optional[list[float]] = Field(None, description="两版本适用温区交集")
    max_correction_delta_c: Optional[float] = Field(
        None, description="交集温区内两版本修正值的最大绝对偏差 (degC)"
    )
