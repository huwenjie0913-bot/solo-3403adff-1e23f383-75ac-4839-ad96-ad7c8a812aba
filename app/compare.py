"""两次校核并列查看：比对判定签名集合与指标差异。

可用于：批次重审（同批次多次校核）与配方版次/规则版本对比。
判定签名 = (code, stage, zone, sensor)；同一签名仍存在但首越界时刻或
观测值变化的归入 changed。
"""
from __future__ import annotations

from .schemas import CompareResponse, EvaluationReport, FindingDiff


def signature(f) -> tuple:
    return (f.code, f.stage_id, f.zone_id, f.sensor_id)


def _diff(f, g) -> FindingDiff:
    return FindingDiff(
        code=f.code,
        severity=f.severity,
        stage_id=f.stage_id,
        zone_id=f.zone_id,
        sensor_id=f.sensor_id,
        first_at_a=f.first_at,
        first_at_b=g.first_at if g else None,
        observed_a=f.observed,
        observed_b=g.observed if g else None,
        limit_value=f.limit_value,
        metric=f.metric,
        unit=f.unit,
        message_a=f.message,
        message_b=g.message if g else None,
    )


def compare_reports(a: EvaluationReport, b: EvaluationReport | None) -> CompareResponse:
    """A 为当前校核、B 为基线；added=A 有而 B 无，removed 反之。"""
    map_a = {signature(f): f for f in a.findings}
    map_b = {signature(f): f for f in b.findings} if b else {}

    added = [_diff(map_a[k], None) for k in map_a.keys() - map_b.keys()]
    removed = [_diff(map_b[k], None) for k in map_b.keys() - map_a.keys()]
    changed = []
    for k in map_a.keys() & map_b.keys():
        f, g = map_a[k], map_b[k]
        if (f.first_at != g.first_at or f.observed != g.observed
                or f.severity != g.severity):
            changed.append(_diff(f, g))

    keyf = lambda d: (d.code, d.stage_id or "", d.zone_id or "", d.sensor_id or "")
    added.sort(key=keyf)
    removed.sort(key=keyf)
    changed.sort(key=keyf)

    return CompareResponse(
        batch_no=a.batch_no,
        a=a,
        b=b,
        status_a=a.status,
        status_b=b.status if b else None,
        same_verdict=bool(b is not None and a.status == b.status
                          and not added and not removed and not changed),
        added=added,
        removed=removed,
        changed=changed,
    )
