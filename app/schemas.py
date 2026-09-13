"""请求/响应 Pydantic 模型。

所有时间戳统一使用 ISO 8601；温度输入支持 c/f/k 三种量纲，
入引擎后一律换算为摄氏度 (degC)，速率单位为 degC/min。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    value_c: float
    unit_in: TemperatureUnit
    raw_value: float
    role: str = Field(..., description="该读数在本判定中的作用，如 before/after/peak/gap_left/gap_right")


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


class EvaluationReport(BaseModel):
    evaluation_id: Optional[int] = None
    batch_no: str
    furnace_id: Optional[str]
    recipe_id: str
    recipe_version: str
    rule_version: str
    status: Literal["PASS", "FAIL", "INPUT_ERROR"]
    first_violation: Optional[Finding] = None
    findings: list[Finding] = Field(default_factory=list)
    stages: list[StageResult] = Field(default_factory=list)
    input_summary: dict
    evaluated_at: datetime


# ---------- 并列查看 ----------

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
