"""边界情况：稀疏/部分覆盖读数、多单位、阶段无数据。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas import EvaluateRequest, ReadingIn, StageIn, StageKind, ZoneIn
from app.engine import evaluate

T0 = datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)


def _sparse_request():
    # 仅在保温段末尾给 3 个读数；升温/降温段完全无覆盖；
    # 且首/末根（阶段边界）与读数时刻错开，制造“仅单侧已知”的段。
    readings = [
        ReadingIn(t=T0 + timedelta(seconds=3540), sensor_id="T1", value=900, unit="c"),
        ReadingIn(t=T0 + timedelta(seconds=3540), sensor_id="T2",
                  value=1652.0, unit="f"),
        ReadingIn(t=T0 + timedelta(seconds=3600), sensor_id="T1", value=905, unit="c"),
        ReadingIn(t=T0 + timedelta(seconds=3600), sensor_id="T2",
                  value=1661.0, unit="f"),
    ]
    stages = [
        StageIn(stage_id="S1", kind=StageKind.HEAT, start=T0,
                end=T0 + timedelta(seconds=1800), max_heat_rate_c_min=30),
        StageIn(stage_id="S2", kind=StageKind.SOAK,
                start=T0 + timedelta(seconds=1800),
                end=T0 + timedelta(seconds=3600),
                target_low_c=880, target_high_c=920, min_soak_seconds=1500),
        StageIn(stage_id="S3", kind=StageKind.COOL,
                start=T0 + timedelta(seconds=3600),
                end=T0 + timedelta(seconds=5400), max_cool_rate_c_min=200),
    ]
    return EvaluateRequest(
        batch_no="B-SPARSE", recipe_id="R", recipe_version="1",
        zones=[ZoneIn(zone_id="Z1", sensor_ids=["T1", "T2"])],
        stages=stages, readings=readings,
        max_overshoot_c=10, sensor_tolerance_c=3)


def test_sparse_readings_do_not_crash_and_report_no_data():
    rep = evaluate(_sparse_request())
    assert rep.status == "FAIL"
    # 升温/降温段无数据 → SENSOR_STAGE_NO_DATA；保温段数据存在但严重不足 → SOAK_TOO_SHORT
    codes = {(f.code, f.stage_id) for f in rep.findings}
    assert ("SENSOR_STAGE_NO_DATA", "S1") in codes
    assert ("SENSOR_STAGE_NO_DATA", "S3") in codes
    assert ("SOAK_TOO_SHORT", "S2") in codes
    # 华氏度测点换算后与摄氏度测点同带，不应产生均匀性判定
    assert not any(f.code == "NONUNIFORM" and f.stage_id == "S2" for f in rep.findings)


def test_single_reading_sensor_does_not_crash():
    req = _sparse_request()
    # 只留一个读数：所有阶段均单侧/无覆盖
    req.readings = [req.readings[0]]
    rep = evaluate(req)
    assert rep.status == "FAIL"
    assert all(f.code != "INTERNAL" for f in rep.findings)


def test_naive_and_aware_timestamps_mix_ok():
    req = _sparse_request()
    # naive 时间戳按 UTC 处理，与 aware 混用不报错
    req.readings[0] = req.readings[0].model_copy(update={
        "t": datetime(2026, 9, 13, 8, 59, 0)})
    rep = evaluate(req)
    assert rep.evaluated_at is not None
