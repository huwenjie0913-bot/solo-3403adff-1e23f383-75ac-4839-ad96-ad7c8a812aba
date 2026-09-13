"""合成标准三段炉温曲线（升温-保温-降温），供各用例注入缺陷。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas import EvaluateRequest, ReadingIn, StageIn, StageKind, ZoneIn

T0 = datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)
SAMPLE = 60  # 秒


def iso(t: datetime) -> datetime:
    return t.astimezone(timezone.utc)


def _profile_temperature(t_rel: float, *, spike: dict | None = None) -> float:
    """以秒计的相对时刻 → 期望温度。0-1800 升温到 900，1800-3600 保温。"""
    if t_rel < 1800:
        temp = 20.0 + (900.0 - 20.0) * t_rel / 1800.0
    elif t_rel <= 3600:
        temp = 900.0
    else:
        temp = max(20.0, 900.0 - 150.0 / 60.0 * (t_rel - 3600.0))
    if spike and abs(t_rel - spike["at"]) < 1e-9:
        temp += spike["amount"]
    return temp


def build_request(
    *,
    batch_no: str = "B2026-0913-01",
    recipe_version: str = "R-A",
    rule_version: str | None = None,
    sensor_ids=("Z1-TC1", "Z1-TC2"),
    duration: float = 5400.0,
    spike: dict | None = None,
    soak_short: bool = False,
    cool_fast: bool = False,
    drop_gap: tuple | None = None,
    fahrenheit_sensor: str | None = None,
    implausible_sensor: str | None = None,
    max_overshoot_c: float = 10.0,
    tolerance: float = 3.0,
    end_soak: float | None = None,
    stages_extra: list | None = None,
) -> EvaluateRequest:
    readings = []
    t_rel = 0.0
    while t_rel <= duration:
        if drop_gap is None or not (drop_gap[0] < t_rel < drop_gap[1]):
            for sid in sensor_ids:
                val = _profile_temperature(t_rel, spike=(spike if sid == spike_sensor(spike) else None))
                if soak_short and sid == sensor_ids[0] and 2400 <= t_rel <= 3000:
                    # 缓慢下漂到 873（acc_low=875 之下），单分钟变化 27°C/min
                    # 低于速率限值 30，避免干扰保温时长判定
                    val = 873.0
                if cool_fast and t_rel >= 3600:
                    val = _profile_temperature(3600.0) - 400.0 / 60.0 * (t_rel - 3600.0)
                    val = max(val, 20.0)
                if implausible_sensor == sid and t_rel == 600.0:
                    val = 99999.0
                unit = "f" if sid == fahrenheit_sensor else "c"
                if unit == "f":
                    val = val * 9.0 / 5.0 + 32.0
                readings.append(ReadingIn(t=iso(T0 + timedelta(seconds=t_rel)),
                                          sensor_id=sid, value=round(val, 3), unit=unit))
        t_rel += SAMPLE

    soak_end = end_soak if end_soak is not None else 3600.0
    stages = [
        StageIn(stage_id="S1", name="升温", kind=StageKind.HEAT,
                start=iso(T0), end=iso(T0 + timedelta(seconds=1800)),
                max_heat_rate_c_min=30.0),
        StageIn(stage_id="S2", name="保温", kind=StageKind.SOAK,
                start=iso(T0 + timedelta(seconds=1800)),
                end=iso(T0 + timedelta(seconds=soak_end)),
                target_low_c=880.0, target_high_c=920.0,
                max_heat_rate_c_min=30.0, max_cool_rate_c_min=30.0,
                min_soak_seconds=1500.0),
        StageIn(stage_id="S3", name="降温", kind=StageKind.COOL,
                start=iso(T0 + timedelta(seconds=soak_end)),
                end=iso(T0 + timedelta(seconds=duration)),
                max_cool_rate_c_min=200.0),
    ]
    if stages_extra:
        stages.extend(stages_extra)

    zones = [ZoneIn(zone_id="Z1", sensor_ids=list(sensor_ids))]

    return EvaluateRequest(
        batch_no=batch_no, furnace_id="F-07", recipe_id="REC-42CrMo",
        recipe_version=recipe_version, rule_version=rule_version,
        zones=zones, stages=stages, readings=readings,
        max_heat_rate_c_min=30.0, max_cool_rate_c_min=200.0,
        max_overshoot_c=max_overshoot_c, sensor_tolerance_c=tolerance)


def spike_sensor(spike):
    return (spike or {}).get("sensor", "Z1-TC1")
