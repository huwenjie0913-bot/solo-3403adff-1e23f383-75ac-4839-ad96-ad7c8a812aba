"""引擎级测试：不经过 HTTP，直接校核合成曲线。"""
from __future__ import annotations

import pytest

from app.engine import evaluate

from .factory import build_request


def test_good_curve_passes():
    report = evaluate(build_request())
    assert report.status == "PASS", [f.message for f in report.findings]
    soak = next(s for s in report.stages if s.stage_id == "S2")
    zone = soak.zones[0]
    # 1800s 保温，全程同时在带，均匀性温差≈0
    assert zone.simultaneous_in_band_seconds == pytest.approx(1800, abs=2)
    assert zone.overtemp_seconds == 0
    assert zone.max_spread_c < 0.1
    heat = next(s for s in report.stages if s.stage_id == "S1")
    rates = [sr.max_rate_c_min for zr in heat.zones for sr in zr.sensors]
    assert all(r is not None and r < 30.1 for r in rates)


def test_short_overtemp_rule_versions_differ():
    # 单点冲到 929°C：超过 1.1 硬界 H+O-t=927，但不超 1.0 硬界 H+O=930；
    # 偏差 29°C 超有效均匀性界（限值 2t+t=9），故 1.0 仍因 NONUNIFORM 判废，
    # 但不会出现 OVERTEMP —— 两个规则版本对“短时超温”的区分点正在于此。
    spike = {"at": 3540.0, "amount": 29.0}
    req11 = build_request(spike=spike, rule_version="1.1.0")
    req10 = build_request(spike=spike, rule_version="1.0.0", batch_no="B-X")
    rep11 = evaluate(req11)
    rep10 = evaluate(req10)

    codes11 = {f.code for f in rep11.findings}
    codes10 = {f.code for f in rep10.findings}
    assert "OVERTEMP" in codes11
    assert rep11.status == "FAIL"
    assert "OVERTEMP" not in codes10
    assert rep10.status == "FAIL"
    assert "NONUNIFORM" in codes10

    over = next(f for f in rep11.findings if f.code == "OVERTEMP")
    assert over.limit_value == 927.0
    assert over.observed == 929.0
    assert over.sensor_id == "Z1-TC1"
    roles = {c.role for c in over.citations}
    # 越界点由线性求根定位（3540s 之前），before/after 夹住；峰值读数 role=peak
    assert {"overtemp_before", "overtemp_after"} & roles
    assert "peak" in roles
    assert any(c.sensor_id == "Z1-TC1" for c in over.citations)
    assert over.first_at is not None


def test_first_violation_is_earliest_error():
    # 同时制造：升温段坏点（600s）+ 保温段超温（3540s）
    req = build_request(spike={"at": 3540.0, "amount": 50.0},
                        implausible_sensor="Z1-TC1", batch_no="B-FIRST")
    rep = evaluate(req)
    assert rep.status == "FAIL"
    assert rep.first_violation is not None
    # 坏点在 600s，是最早的 error
    assert rep.first_violation.code == "IMPLAUSIBLE_TEMPERATURE"
    codes = {f.code for f in rep.findings}
    assert "OVERTEMP" in codes


def test_cool_rate_exceeded_and_citation():
    rep = evaluate(build_request(cool_fast=True, batch_no="B-COOL"))
    finding = next(f for f in rep.findings if f.code == "COOL_RATE_EXCEEDED")
    assert finding.stage_id == "S3"
    assert finding.observed > 200.0
    assert len(finding.citations) >= 2
    assert finding.citations[0].t < finding.citations[-1].t


def test_soak_too_short():
    rep = evaluate(build_request(soak_short=True, batch_no="B-SOAK"))
    assert rep.status == "FAIL"
    f = next(x for x in rep.findings
             if x.code == "SOAK_TOO_SHORT" and x.sensor_id == "Z1-TC1")
    assert f.stage_id == "S2"
    assert f.observed < 1500.0
    # 引用进入/离开两个时刻的读数（精确命中采样点时 role 为 band_enter/band_left）
    roles = {c.role for c in f.citations}
    assert any("band_" in r for r in roles)


def test_sensor_offline_error():
    # 300s 空洞（5 倍间隔，超过 offline_factor=3 但不超 error_factor=6 → warning）
    rep = evaluate(build_request(drop_gap=(2400, 2700), batch_no="B-GAP"))
    assert any(f.code == "SENSOR_OFFLINE" and f.severity.value == "warning"
               for f in rep.findings)
    # 600s 空洞（10 倍，超过 6 倍 → error）
    rep2 = evaluate(build_request(drop_gap=(2400, 3000), batch_no="B-GAP2"))
    assert rep2.status == "FAIL"
    off = next(f for f in rep2.findings
               if f.code == "SENSOR_OFFLINE" and f.severity.value == "error")
    assert {c.role for c in off.citations} == {"gap_left", "gap_right"}
    assert off.metric == "gap_s"


def test_fahrenheit_input_converted():
    rep = evaluate(build_request(fahrenheit_sensor="Z1-TC2", batch_no="B-F"))
    assert rep.status == "PASS", [f.message for f in rep.findings]
    assert rep.input_summary["units_seen"] == ["c", "f"]
    soak = next(s for s in report_stages(rep) if s.stage_id == "S2")
    tc2 = next(x for x in soak.zones[0].sensors if x.sensor_id == "Z1-TC2")
    assert tc2.in_band_seconds > 1700


def report_stages(rep):
    return rep.stages


def test_implausible_dropped_but_other_sensor_continues():
    rep = evaluate(build_request(implausible_sensor="Z1-TC1", batch_no="B-BAD"))
    assert rep.status == "FAIL"
    bad = next(f for f in rep.findings if f.code == "IMPLAUSIBLE_TEMPERATURE")
    assert bad.citations[0].value_c > 2000
    # TC2 全程正常，保温指标照常产出
    soak = next(s for s in rep.stages if s.stage_id == "S2")
    tc2 = next(x for x in soak.zones[0].sensors if x.sensor_id == "Z1-TC2")
    assert tc2.longest_in_band_seconds > 1700


def test_two_zones_evaluated_independently():
    """两个炉区：一区短时超温，另一区正常——平均值掩盖不住。"""
    from app.schemas import ZoneIn

    req = build_request(batch_no="B-MULTI", spike={"at": 3540.0, "amount": 50.0})
    # 复制成两个炉区：Z1 带尖峰，Z2 干净
    z2 = ZoneIn(zone_id="Z2", sensor_ids=["Z2-TC1", "Z2-TC2"])
    clean = build_request()
    for r in clean.readings:
        req.readings.append(r.model_copy(update={
            "sensor_id": {"Z1-TC1": "Z2-TC1", "Z1-TC2": "Z2-TC2"}[r.sensor_id]}))
    req.zones.append(z2)

    rep = evaluate(req)
    assert rep.status == "FAIL"
    over = [f for f in rep.findings if f.code == "OVERTEMP"]
    assert {f.zone_id for f in over} == {"Z1"}
    assert {f.sensor_id for f in over} == {"Z1-TC1"}

    soak = next(s for s in rep.stages if s.stage_id == "S2")
    by_zone = {z.zone_id: z for z in soak.zones}
    assert by_zone["Z1"].overtemp_seconds > 0
    assert by_zone["Z2"].overtemp_seconds == 0
    # 两区结果各自独立列出
    assert {z.zone_id for z in soak.zones} == {"Z1", "Z2"}
