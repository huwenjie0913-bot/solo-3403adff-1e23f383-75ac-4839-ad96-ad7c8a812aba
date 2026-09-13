"""校核引擎：时序/量纲预检 + 逐阶段速率、保温带、超温、均匀性计算。

方法要点
~~~~~~~~
* 读数先按量纲换算到摄氏度，传感器时间序列严格递增、去重，再做物理可信
  范围检查（量纲错误/采集坏点丢弃并判 IMPLAUSIBLE_TEMPERATURE）。
* 阶段窗口内，把所有传感器的采样时刻合并为统一时间轴；掉线区间
  （相邻原始读数间隔 > offline_gap_factor × 标称采样间隔）不做插值，
  状态记为 unknown，均匀性/保温时长不计入该区间。
* 时间轴每个线段内各传感器温度线性，用求根切分精确计算：
  进/出保温带时刻、保温累计与最长连续时长、超温累计时长、炉区温差
  （两两测点差值的越限根），无需粗糙地按中点近似。
* 每条判定携带引用的原始读数（提交值 + 摄氏度换算值 + 在判定中的作用）。
"""
from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .rules import (
    DEFAULT_RULE_VERSION,
    PHYSICAL_MAX_C,
    PHYSICAL_MIN_C,
    RULE_VERSIONS,
)
from .schemas import (
    CitedReading,
    EvaluationReport,
    Finding,
    ReadingIn,
    SensorStageResult,
    Severity,
    StageIn,
    StageResult,
    StageKind,
    TemperatureUnit,
    ZoneIn,
    ZoneStageResult,
)

# ---------- 基础工具 ----------


def as_utc(dt: datetime) -> datetime:
    """naive 时间戳按 UTC 处理；aware 时间戳统一到 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ep(dt: datetime) -> float:
    return as_utc(dt).timestamp()


def dt_of(t: float) -> datetime:
    return datetime.fromtimestamp(t, tz=timezone.utc)


def to_celsius(value: float, unit: TemperatureUnit) -> float:
    if unit == TemperatureUnit.C:
        return value
    if unit == TemperatureUnit.F:
        return (value - 32.0) * 5.0 / 9.0
    return value - 273.15


@dataclass
class RawPoint:
    t: float
    c: float
    raw: float
    unit: TemperatureUnit


@dataclass
class Series:
    sensor_id: str
    points: list[RawPoint] = field(default_factory=list)

    def times(self) -> list[float]:
        return [p.t for p in self.points]

    def at(self, t: float, max_gap: float) -> Optional[float]:
        """t 时刻温度；需要两侧真实读数且间隔不超过掉线阈值，否则 None。"""
        ts = self.times()
        i = bisect.bisect_left(ts, t)
        if i < len(ts) and ts[i] == t:
            return self.points[i].c
        if i == 0 or i == len(ts):
            return None
        p0, p1 = self.points[i - 1], self.points[i]
        if p1.t - p0.t > max_gap:
            return None
        frac = (t - p0.t) / (p1.t - p0.t)
        return p0.c + frac * (p1.c - p0.c)

    def bracket_raw(self, t: float) -> list[tuple[RawPoint, str]]:
        """返回夹住 t 的原始读数（精确命中返回单点），附 role。"""
        ts = self.times()
        i = bisect.bisect_left(ts, t)
        if i < len(ts) and ts[i] == t:
            return [(self.points[i], "at")]
        if i == 0 or i == len(ts):
            return []
        return [(self.points[i - 1], "before"), (self.points[i], "after")]


def cite(series: Series, t: float, role_map: Optional[dict[str, str]] = None) -> list[CitedReading]:
    """构造某时刻的读数引用；role_map 可把 before/after/at 改成业务角色名。"""
    role_map = role_map or {}
    out: list[CitedReading] = []
    for p, role in series.bracket_raw(t):
        out.append(
            CitedReading(
                t=dt_of(p.t),
                sensor_id=series.sensor_id,
                value_c=round(p.c, 4),
                unit_in=p.unit,
                raw_value=p.raw,
                role=role_map.get(role, role),
            )
        )
    return out


# ---------- 判定收集 ----------


class FindingCollector:
    """按 (code, stage, zone, sensor) 去重，保留最早 first_at 与最劣 observed。"""

    def __init__(self) -> None:
        self._items: dict[tuple, Finding] = {}
        self._extra: dict[tuple, dict] = {}

    def add(
        self,
        *,
        code: str,
        severity: Severity,
        message: str,
        stage_id: Optional[str] = None,
        zone_id: Optional[str] = None,
        sensor_id: Optional[str] = None,
        first_at: Optional[float] = None,
        metric: Optional[str] = None,
        limit_value: Optional[float] = None,
        observed: Optional[float] = None,
        unit: Optional[str] = None,
        citations: Optional[list[CitedReading]] = None,
    ) -> None:
        key = (code, stage_id, zone_id, sensor_id)
        current = self._items.get(key)
        if current is None or (
            first_at is not None
            and (current.first_at is None or first_at < ep(current.first_at))
        ):
            finding = Finding(
                code=code,
                severity=severity,
                message=message,
                stage_id=stage_id,
                zone_id=zone_id,
                sensor_id=sensor_id,
                first_at=dt_of(first_at) if first_at is not None else None,
                metric=metric,
                limit_value=round(limit_value, 4) if limit_value is not None else None,
                observed=round(observed, 4) if observed is not None else None,
                unit=unit,
                citations=citations or [],
            )
            self._items[key] = finding
            self._extra[key] = {}
            return
        # 已存在：仅更新更极端的 observed
        if observed is not None:
            cur = current.observed
            if cur is None or observed > cur:
                current.observed = round(observed, 4)

    def all(self) -> list[Finding]:
        rank = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
        return sorted(
            self._items.values(),
            key=lambda f: (
                rank[f.severity],
                ep(f.first_at) if f.first_at is not None else float("inf"),
                f.code,
                f.stage_id or "",
                f.zone_id or "",
                f.sensor_id or "",
            ),
        )


# ---------- 预检 ----------


@dataclass
class PrecheckResult:
    series_map: dict[str, Series]
    nominal_gap: Optional[float]
    fatal: bool


def _precheck(req, fc: FindingCollector) -> PrecheckResult:
    """时序、量纲、采样间距与登记关系检查。fatal=True 时不再做阶段校核。"""
    fatal = False

    # 炉区 / 传感器登记
    zone_ids: set[str] = set()
    sensor_zone: dict[str, str] = {}
    for z in req.zones:
        if z.zone_id in zone_ids:
            fc.add(code="DUPLICATE_ZONE_ID", severity=Severity.ERROR,
                   message=f"炉区编号重复：{z.zone_id}", zone_id=z.zone_id)
            fatal = True
        zone_ids.add(z.zone_id)
        for sid in z.sensor_ids:
            if sid in sensor_zone:
                fc.add(code="SENSOR_IN_MULTIPLE_ZONES", severity=Severity.ERROR,
                       message=f"传感器 {sid} 同时登记在多个炉区", sensor_id=sid)
                fatal = True
            sensor_zone[sid] = z.zone_id

    # 阶段
    stages = sorted(req.stages, key=lambda s: ep(s.start))
    stage_ids: set[str] = set()
    for s in stages:
        if s.stage_id in stage_ids:
            fc.add(code="DUPLICATE_STAGE_ID", severity=Severity.ERROR,
                   message=f"阶段编号重复：{s.stage_id}", stage_id=s.stage_id)
            fatal = True
        stage_ids.add(s.stage_id)
        if s.kind == StageKind.SOAK and (s.target_low_c is None or s.target_high_c is None):
            fc.add(code="SOAK_BAND_REQUIRED", severity=Severity.ERROR,
                   message=f"保温阶段 {s.stage_id} 必须提供目标温度带",
                   stage_id=s.stage_id)
            fatal = True
    for s0, s1 in zip(stages, stages[1:]):
        if ep(s1.start) < ep(s0.end):
            fc.add(code="STAGE_OVERLAP", severity=Severity.ERROR,
                   message=f"阶段时间窗重叠：{s0.stage_id} 与 {s1.stage_id}",
                   stage_id=s1.stage_id, first_at=ep(s1.start))
            fatal = True

    # 读数：按传感器分组（保持提交顺序），先查时序，再换算/查量纲
    raw_groups: dict[str, list[ReadingIn]] = {}
    unknown: dict[str, list[ReadingIn]] = {}
    for r in req.readings:
        if r.sensor_id not in sensor_zone:
            unknown.setdefault(r.sensor_id, []).append(r)
        else:
            raw_groups.setdefault(r.sensor_id, []).append(r)

    for sid, items in unknown.items():
        cites = [
            CitedReading(t=as_utc(r.t), sensor_id=sid,
                         value_c=round(to_celsius(r.value, r.unit), 4),
                         unit_in=r.unit, raw_value=r.value, role="unknown_sensor_reading")
            for r in items[:2]
        ]
        fc.add(code="UNKNOWN_SENSOR", severity=Severity.ERROR,
               message=f"读数引用了未登记传感器 {sid}（{len(items)} 条）",
               sensor_id=sid, first_at=ep(as_utc(items[0].t)), citations=cites)
        fatal = True

    series_map: dict[str, Series] = {}
    for sid, items in raw_groups.items():
        ordered = [ep(r.t) for r in items]
        if any(b < a for a, b in zip(ordered, ordered[1:])):
            fc.add(code="READINGS_NOT_ORDERED", severity=Severity.ERROR,
                   message=f"传感器 {sid} 的读数未按采样时刻递增提交", sensor_id=sid)
            fatal = True
        if len(set(ordered)) != len(ordered):
            fc.add(code="DUPLICATE_SAMPLE", severity=Severity.ERROR,
                   message=f"传感器 {sid} 存在重复采样时刻", sensor_id=sid)
            fatal = True

        series = Series(sid)
        bad_cites: list[CitedReading] = []
        for r in sorted(items, key=lambda x: ep(x.t)):
            c = to_celsius(r.value, r.unit)
            if c < PHYSICAL_MIN_C or c > PHYSICAL_MAX_C:
                if len(bad_cites) < 4:
                    bad_cites.append(CitedReading(
                        t=as_utc(r.t), sensor_id=sid, value_c=round(c, 4),
                        unit_in=r.unit, raw_value=r.value, role="implausible"))
                continue
            series.points.append(RawPoint(ep(as_utc(r.t)), c, r.value, r.unit))
        if bad_cites:
            fc.add(code="IMPLAUSIBLE_TEMPERATURE", severity=Severity.ERROR,
                   message=f"传感器 {sid} 存在超物理可信范围读数，已剔除此类坏点后继续校核",
                   zone_id=sensor_zone.get(sid), sensor_id=sid,
                   first_at=ep(bad_cites[0].t), citations=bad_cites,
                   metric="temperature_c", observed=bad_cites[0].value_c,
                   limit_value=PHYSICAL_MAX_C, unit="degC")
        series_map[sid] = series

    if fatal:
        return PrecheckResult(series_map, None, True)

    # 标称采样间隔
    all_gaps: list[float] = []
    for s in series_map.values():
        ts = s.times()
        all_gaps.extend(b - a for a, b in zip(ts, ts[1:]))
    nominal = req.nominal_sample_seconds
    if nominal is None:
        nominal = statistics.median(all_gaps) if all_gaps else None

    if nominal is not None and nominal > 0:
        warn_gap = nominal * req.max_sample_jitter
        offline_gap = nominal * req.offline_gap_factor
        error_gap = nominal * req.offline_error_factor
        for sid, series in series_map.items():
            for p0, p1 in zip(series.points, series.points[1:]):
                gap = p1.t - p0.t
                if gap <= warn_gap:
                    continue
                citations = [
                    CitedReading(t=dt_of(p0.t), sensor_id=sid, value_c=round(p0.c, 4),
                                 unit_in=p0.unit, raw_value=p0.raw, role="gap_left"),
                    CitedReading(t=dt_of(p1.t), sensor_id=sid, value_c=round(p1.c, 4),
                                 unit_in=p1.unit, raw_value=p1.raw, role="gap_right"),
                ]
                if gap > offline_gap:
                    sev = Severity.ERROR if gap > error_gap else Severity.WARNING
                    fc.add(code="SENSOR_OFFLINE", severity=sev,
                           message=(f"传感器 {sid} 掉线 {gap:.0f}s（阈值 "
                                    f"{offline_gap:.0f}s），掉线区间不插值、不计保温"),
                           zone_id=sensor_zone[sid], sensor_id=sid, first_at=p0.t,
                           metric="gap_s", limit_value=round(offline_gap, 2),
                           observed=round(gap, 2), unit="s", citations=citations)
                else:
                    fc.add(code="SAMPLE_GAP", severity=Severity.WARNING,
                           message=f"传感器 {sid} 采样间距 {gap:.0f}s 超过标称 {nominal:.0f}s "
                                   f"× {req.max_sample_jitter}",
                           zone_id=sensor_zone[sid], sensor_id=sid, first_at=p0.t,
                           metric="gap_s", limit_value=round(warn_gap, 2),
                           observed=round(gap, 2), unit="s", citations=citations)

    # 登记了但全程无读数的传感器
    for z in req.zones:
        for sid in z.sensor_ids:
            if not series_map.get(sid) or not series_map[sid].points:
                fc.add(code="SENSOR_STAGE_NO_DATA", severity=Severity.ERROR,
                       message=f"传感器 {sid} 全程无有效读数，无法校核",
                       zone_id=z.zone_id, sensor_id=sid)

    return PrecheckResult(series_map, nominal, False)


# ---------- 线段求根 ----------


def _frac_for_level(v0: float, v1: float, level: float) -> Optional[float]:
    """线段端点值 v0→v1，求 value==level 的内部比例 (0,1)。"""
    d = v1 - v0
    if d == 0:
        return None
    f = (level - v0) / d
    return f if 0 < f < 1 else None


def _merge_runs(lengths: list[tuple[float, bool]]) -> tuple[float, float]:
    """按顺序的 (段长, 是否满足) 求满足累计与最长连续满足时长。"""
    total = 0.0
    longest = cur = 0.0
    for length, ok in lengths:
        if ok:
            total += length
            cur += length
            longest = max(longest, cur)
        else:
            cur = 0.0
    return total, longest


# ---------- 阶段校核 ----------


def _evaluate_stage(
    stage: StageIn,
    zone: ZoneIn,
    series_map: dict[str, Series],
    req,
    max_gap: Optional[float],
    fc: FindingCollector,
    rule_meta: dict,
) -> ZoneStageResult:
    a, b = ep(stage.start), ep(stage.end)
    tol = req.sensor_tolerance_c
    has_band = stage.target_low_c is not None and stage.target_high_c is not None

    if has_band:
        low, high = stage.target_low_c, stage.target_high_c
        acc_low, acc_high = low - tol, high + tol
        ceiling = high + req.max_overshoot_c
        if rule_meta["overshoot_tolerance_applied"]:
            ceiling -= tol
        # 限值按“两测点真实温差”定义；读数测量误差使读数温差最多偏大 t，
        # 故验收有效限值 = 限值 + tolerance（默认限值 2t 时验收界为 3t）。
        spread_limit = zone.max_uniformity_delta
        if spread_limit is None:
            spread_limit = 2.0 * tol
        eff_spread_limit = spread_limit + tol
    else:
        low = high = acc_low = acc_high = ceiling = eff_spread_limit = None

    zone_res = ZoneStageResult(zone_id=zone.zone_id, sensor_ids=list(zone.sensor_ids))

    # ---- 统一时间轴根：窗口边界 + 窗口内各传感器真实采样点 ----
    roots = {a, b}
    for sid in zone.sensor_ids:
        series = series_map.get(sid)
        if series is None:
            continue
        for t in series.times():
            if a < t < b:
                roots.add(t)
    roots = sorted(roots)

    # 每传感器在根上的值（None=未知）
    known: dict[str, list[Optional[float]]] = {}
    for sid in zone.sensor_ids:
        series = series_map.get(sid)
        vals = [series.at(t, max_gap) if series and max_gap is not None else None
                for t in roots]
        known[sid] = vals

    sensor_stats: dict[str, dict] = {}

    # 每个根索引区间做切分扫描
    # 累积器
    acc = {sid: {"in_len": [], "over_len": [], "max_temp": None,
                 "first_in": None, "last_in": None, "first_over": None,
                 "known_anywhere": False}
           for sid in zone.sensor_ids}
    zone_all_in_len: list[tuple[float, bool]] = []
    zone_uniform_len: list[tuple[float, bool]] = []
    zone_over_len: list[tuple[float, bool]] = []
    zone_max_spread = None
    zone_first_nonuniform: Optional[float] = None
    offline_seconds = 0.0

    for k in range(len(roots) - 1):
        r0, r1 = roots[k], roots[k + 1]
        if r1 <= r0:
            continue
        # 段端点值：两端各自判已知，缺任一侧的传感器不做段内插值（不跨掉线外推）
        known0 = {sid: known[sid][k] is not None for sid in zone.sensor_ids}
        known1 = {sid: known[sid][k + 1] is not None for sid in zone.sensor_ids}
        both = [sid for sid in zone.sensor_ids if known0[sid] and known1[sid]]
        v0 = {sid: known[sid][k] for sid in both}
        v1 = {sid: known[sid][k + 1] for sid in both}
        all_known = len(both) == len(zone.sensor_ids)
        if not all_known:
            offline_seconds += r1 - r0

        # 段内切分根（比例）
        cuts: set[float] = set()
        if has_band:
            for sid in v0:
                for level in (acc_low, acc_high, ceiling):
                    f = _frac_for_level(v0[sid], v1[sid], level)
                    if f is not None:
                        cuts.add(f)
            if all_known:
                sids = list(v0)
                for i in range(len(sids)):
                    for j in range(i + 1, len(sids)):
                        si, sj = sids[i], sids[j]
                        d0, d1 = v0[si] - v0[sj], v1[si] - v1[sj]
                        for target in (eff_spread_limit, -eff_spread_limit):
                            if d1 != d0:
                                f = (target - d0) / (d1 - d0)
                                if 0 < f < 1:
                                    cuts.add(f)

        cuts.add(0.0)
        cuts.add(1.0)
        cuts_sorted = sorted(cuts)

        def values_at(f: float) -> dict[str, float]:
            return {sid: v0[sid] + f * (v1[sid] - v0[sid]) for sid in v0}

        for cf0, cf1 in zip(cuts_sorted, cuts_sorted[1:]):
            length = (cf1 - cf0) * (r1 - r0)
            if length <= 0:
                continue
            mid = (cf0 + cf1) / 2
            vm = values_at(mid)
            ev0, ev1 = values_at(cf0), values_at(cf1)
            tl0, tl1 = r0 + cf0 * (r1 - r0), r0 + cf1 * (r1 - r0)

            for sid in zone.sensor_ids:
                st = acc[sid]
                if sid not in vm:
                    st["in_len"].append((length, False))
                    st["over_len"].append((length, False))
                    continue
                val = vm[sid]
                # 线性段峰值/谷值在端点（采样点），端点也要计入
                endpoint_peak = max(ev0[sid], ev1[sid])
                st["known_anywhere"] = True
                st["max_temp"] = (max(st["max_temp"], endpoint_peak)
                                  if st["max_temp"] is not None else endpoint_peak)
                if has_band:
                    in_band = acc_low <= val <= acc_high
                    st["in_len"].append((length, in_band))
                    if in_band:
                        # 首/末时刻在段级循环后用求根精化，这里先记粗略值
                        if st["first_in"] is None:
                            st["first_in"] = tl0
                        st["last_in"] = tl1
                    over = val > ceiling
                    st["over_len"].append((length, over))
                    if over and st["first_over"] is None:
                        st["first_over"] = tl0

            if has_band and all_known:
                vals_m = list(vm.values())
                spread = max(vals_m) - min(vals_m)
                zone_max_spread = spread if zone_max_spread is None else max(zone_max_spread, spread)
                all_in = all(acc_low <= v <= acc_high for v in vals_m)
                uniform = all_in and spread <= eff_spread_limit
                zone_all_in_len.append((length, all_in))
                zone_uniform_len.append((length, uniform))
                if not uniform and zone_first_nonuniform is None:
                    zone_first_nonuniform = tl0
                zone_over_len.append((length, any(v > ceiling for v in vals_m)))
            elif has_band:
                zone_all_in_len.append((length, False))
                zone_uniform_len.append((length, False))
                zone_over_len.append((length, any(v > ceiling for v in vm.values())))

        # 段端点也要计入 max_temp / max_spread（线性极值在端点）
        if has_band and all_known:
            for vals in (v0, v1):
                spread = max(vals.values()) - min(vals.values())
                zone_max_spread = spread if zone_max_spread is None else max(zone_max_spread, spread)

    # ---- 精化首/末进带时刻（用段内求根，而不是子段起点） ----
    def refine_band_times(sid: str, st: dict) -> None:
        if st["first_in"] is None:
            return
        # 在粗略 first_in 所在原始线段上求 acc_low/acc_high 的根
        for idx in range(len(roots) - 1):
            r0, r1 = roots[idx], roots[idx + 1]
            x0, x1 = known[sid][idx], known[sid][idx + 1]
            if x0 is None or x1 is None:
                continue
            # 首进：从带外到带内
            inside0 = acc_low <= x0 <= acc_high
            inside1 = acc_low <= x1 <= acc_high
            if not inside0 and inside1 and st["first_in"] is not None and r0 <= st["first_in"] <= r1:
                target = acc_low if x1 >= x0 else acc_high
                f = _frac_for_level(x0, x1, target)
                if f is not None:
                    st["first_in"] = r0 + f * (r1 - r0)
            if st["last_in"] is not None and r0 <= st["last_in"] <= r1 and inside0 and not inside1:
                target = acc_low if x1 < acc_low else acc_high
                f = _frac_for_level(x0, x1, target)
                if f is not None:
                    st["last_in"] = r0 + f * (r1 - r0)

    # ---- 每传感器结果 + 速率 ----
    heat_limit = stage.max_heat_rate_c_min
    if heat_limit is None:
        heat_limit = req.max_heat_rate_c_min
    cool_limit = stage.max_cool_rate_c_min
    if cool_limit is None:
        cool_limit = req.max_cool_rate_c_min

    for sid in zone.sensor_ids:
        series = series_map.get(sid)
        st = acc[sid]
        res = SensorStageResult(sensor_id=sid)
        sensor_stats[sid] = st

        # 窗口内按真实采样点划分算速率（掉线段不计）
        partition = [a]
        if series:
            partition.extend(t for t in series.times() if a < t < b)
        partition.append(b)
        max_rate = min_rate = None
        pairs = 0
        for t0, t1 in zip(partition, partition[1:]):
            x0 = series.at(t0, max_gap) if series else None
            x1 = series.at(t1, max_gap) if series else None
            if x0 is None or x1 is None or t1 <= t0:
                res.notes.append(f"{t0:.0f}-{t1:.0f} 区间含掉线/缺测，速率未计")
                continue
            rate = (x1 - x0) / ((t1 - t0) / 60.0)
            pairs += 1
            max_rate = rate if max_rate is None else max(max_rate, rate)
            min_rate = rate if min_rate is None else min(min_rate, rate)

            def rate_citations() -> list[CitedReading]:
                return cite(series, t0, {"before": "rate_start_before", "after": "rate_start_after",
                                         "at": "rate_start"}) + cite(
                    series, t1, {"before": "rate_end_before", "after": "rate_end_after",
                                 "at": "rate_end"})

            if stage.kind == StageKind.HEAT and heat_limit is not None and rate > heat_limit:
                fc.add(code="HEAT_RATE_EXCEEDED", severity=Severity.ERROR,
                       message=f"阶段 {stage.stage_id} 传感器 {sid} 升温速率 "
                               f"{rate:.2f}°C/min > 限值 {heat_limit:.2f}",
                       stage_id=stage.stage_id, zone_id=zone.zone_id, sensor_id=sid,
                       first_at=t0, metric="heat_rate_c_min", limit_value=heat_limit,
                       observed=rate, unit="degC/min", citations=rate_citations())
            elif stage.kind == StageKind.COOL and cool_limit is not None and rate < -cool_limit:
                fc.add(code="COOL_RATE_EXCEEDED", severity=Severity.ERROR,
                       message=f"阶段 {stage.stage_id} 传感器 {sid} 降温速率 "
                               f"{abs(rate):.2f}°C/min > 限值 {cool_limit:.2f}",
                       stage_id=stage.stage_id, zone_id=zone.zone_id, sensor_id=sid,
                       first_at=t0, metric="cool_rate_c_min", limit_value=cool_limit,
                       observed=abs(rate), unit="degC/min", citations=rate_citations())
            elif stage.kind == StageKind.SOAK:
                if heat_limit is not None and rate > heat_limit:
                    fc.add(code="HEAT_RATE_EXCEEDED", severity=Severity.ERROR,
                           message=f"保温阶段 {stage.stage_id} 传感器 {sid} 回升温速率 "
                                   f"{rate:.2f}°C/min > 限值 {heat_limit:.2f}",
                           stage_id=stage.stage_id, zone_id=zone.zone_id, sensor_id=sid,
                           first_at=t0, metric="heat_rate_c_min", limit_value=heat_limit,
                           observed=rate, unit="degC/min", citations=rate_citations())
                if cool_limit is not None and rate < -cool_limit:
                    fc.add(code="COOL_RATE_EXCEEDED", severity=Severity.ERROR,
                           message=f"保温阶段 {stage.stage_id} 传感器 {sid} 降温速率 "
                                   f"{abs(rate):.2f}°C/min > 限值 {cool_limit:.2f}",
                           stage_id=stage.stage_id, zone_id=zone.zone_id, sensor_id=sid,
                           first_at=t0, metric="cool_rate_c_min", limit_value=cool_limit,
                           observed=abs(rate), unit="degC/min", citations=rate_citations())

        res.max_rate_c_min = round(max_rate, 4) if max_rate is not None else None
        res.min_rate_c_min = round(min_rate, 4) if min_rate is not None else None
        res.rate_pairs = pairs

        if has_band:
            refine_band_times(sid, st)
            in_total, in_longest = _merge_runs(st["in_len"])
            over_total, _ = _merge_runs(st["over_len"])
            res.entered_band_at = dt_of(st["first_in"]) if st["first_in"] is not None else None
            res.left_band_at = dt_of(st["last_in"]) if st["last_in"] is not None else None
            res.in_band_seconds = round(in_total, 2)
            res.longest_in_band_seconds = round(in_longest, 2)
            res.overtemp_seconds = round(over_total, 2)
            res.max_temperature_c = round(st["max_temp"], 4) if st["max_temp"] is not None else None
        else:
            res.notes.append("阶段未提供目标温度带，保温/超温指标未计算")

        if not st["known_anywhere"]:
            ctx: list[CitedReading] = []
            if series and series.points:
                before = [p for p in series.points if p.t <= a]
                after = [p for p in series.points if p.t >= b]
                if before:
                    p = before[-1]
                    ctx.append(CitedReading(t=dt_of(p.t), sensor_id=sid, value_c=round(p.c, 4),
                                            unit_in=p.unit, raw_value=p.raw, role="context_before"))
                if after:
                    p = after[0]
                    ctx.append(CitedReading(t=dt_of(p.t), sensor_id=sid, value_c=round(p.c, 4),
                                            unit_in=p.unit, raw_value=p.raw, role="context_after"))
            fc.add(code="SENSOR_STAGE_NO_DATA", severity=Severity.ERROR,
                   message=f"阶段 {stage.stage_id} 内传感器 {sid} 无有效读数（掉线或未覆盖）",
                   stage_id=stage.stage_id, zone_id=zone.zone_id, sensor_id=sid,
                   citations=ctx)

        zone_res.sensors.append(res)

    # ---- 炉区指标 ----
    if has_band:
        all_in_total, all_in_longest = _merge_runs(zone_all_in_len)
        uniform_total, _ = _merge_runs(zone_uniform_len)
        over_total, _ = _merge_runs(zone_over_len)
        window = b - a
        zone_res.max_spread_c = round(zone_max_spread, 4) if zone_max_spread is not None else None
        zone_res.uniform_seconds = round(uniform_total, 2)
        zone_res.nonuniform_seconds = round(max(window - uniform_total - offline_seconds, 0.0), 2)
        zone_res.overtemp_seconds = round(over_total, 2)
        zone_res.simultaneous_in_band_seconds = round(all_in_total, 2)
        zone_res.longest_simultaneous_in_band_seconds = round(all_in_longest, 2)
        if offline_seconds > 0:
            zone_res.notes.append(f"含 {offline_seconds:.0f}s 掉线/缺测区间，均匀性与保温计时已扣除")

        # NONUNIFORM（取最早非均匀时刻的极值传感器做引用）
        if zone_first_nonuniform is not None:
            t = zone_first_nonuniform
            vm = {}
            for sid in zone.sensor_ids:
                v = series_map[sid].at(t, max_gap) if series_map.get(sid) else None
                if v is not None:
                    vm[sid] = v
            citations: list[CitedReading] = []
            if vm:
                hi = max(vm, key=vm.get)
                lo = min(vm, key=vm.get)
                citations += cite(series_map[hi], t, {"before": "spread_high_before",
                                                      "after": "spread_high_after", "at": "spread_high"})
                if lo != hi:
                    citations += cite(series_map[lo], t, {"before": "spread_low_before",
                                                          "after": "spread_low_after", "at": "spread_low"})
            fc.add(code="NONUNIFORM", severity=Severity.ERROR,
                   message=f"阶段 {stage.stage_id} 炉区 {zone.zone_id} 存在非均匀区间 "
                           f"（最大温差 {zone_max_spread:.2f}°C > 有效限值 "
                           f"{eff_spread_limit:.2f}°C，或有测点偏离保温带）",
                   stage_id=stage.stage_id, zone_id=zone.zone_id, first_at=t,
                   metric="spread_c", limit_value=eff_spread_limit,
                   observed=zone_max_spread, unit="degC", citations=citations[:4])

        # OVERTEMP（逐传感器）
        for sid in zone.sensor_ids:
            st = sensor_stats[sid]
            if st["first_over"] is not None:
                series = series_map[sid]
                first_t = st["first_over"]
                # 若起点已在带上方，尝试在原始线段内求精确越界根（允许根落在段尾）
                for idx in range(len(roots) - 1):
                    r0, r1 = roots[idx], roots[idx + 1]
                    x0, x1 = known[sid][idx], known[sid][idx + 1]
                    if x0 is not None and x1 is not None and r0 <= first_t <= r1:
                        d = x1 - x0
                        if d != 0:
                            f = (ceiling - x0) / d
                            if 0 < f <= 1:
                                first_t = r0 + f * (r1 - r0)
                        break
                # 峰值时刻：线性极值在采样点，取窗口内最高原始/插值端点
                peak_val = st["max_temp"]
                peak_time = None
                for k in range(len(roots) - 1):
                    for rr, xx in ((roots[k], known[sid][k]), (roots[k + 1], known[sid][k + 1])):
                        if xx is not None and xx == peak_val:
                            peak_time = rr
                citations = cite(series, first_t, {"before": "overtemp_before",
                                                   "after": "overtemp_after", "at": "overtemp_at"})
                if peak_time is not None:
                    citations += cite(series, peak_time, {"before": "peak_before",
                                                          "after": "peak_after", "at": "peak"})
                seen = set()
                uniq = []
                for c in citations:
                    k = (c.t, c.role)
                    if k not in seen:
                        seen.add(k)
                        uniq.append(c)
                fc.add(code="OVERTEMP", severity=Severity.ERROR,
                       message=f"阶段 {stage.stage_id} 传感器 {sid} 短时超温：峰值 "
                               f"{peak_val:.2f}°C，超硬界 {ceiling:.2f}°C，"
                               f"累计 {_merge_runs(st['over_len'])[0]:.0f}s",
                       stage_id=stage.stage_id, zone_id=zone.zone_id, sensor_id=sid,
                       first_at=first_t, metric="max_temperature_c", limit_value=ceiling,
                       observed=peak_val, unit="degC", citations=uniq[:4])

        # 保温专属判定
        if stage.kind == StageKind.SOAK:
            need = stage.min_soak_seconds
            for sid in zone.sensor_ids:
                series = series_map.get(sid)
                st = sensor_stats[sid]
                if st["first_in"] is None:
                    peak_v = st["max_temp"]
                    citations = []
                    if peak_v is not None and series is not None:
                        peak_time = next(
                            (rr for k in range(len(roots) - 1)
                             for rr, xx in ((roots[k], known[sid][k]),
                                            (roots[k + 1], known[sid][k + 1]))
                             if xx == peak_v),
                            None)
                        if peak_time is not None:
                            citations = cite(series, peak_time,
                                             {"before": "peak_before", "after": "peak_after",
                                              "at": "peak"})
                    fc.add(code="NEVER_ENTERED_BAND", severity=Severity.ERROR,
                           message=f"保温阶段 {stage.stage_id} 传感器 {sid} 从未进入有效保温带 "
                                   f"[{acc_low:.1f}, {acc_high:.1f}]°C",
                           stage_id=stage.stage_id, zone_id=zone.zone_id, sensor_id=sid,
                           first_at=a, metric="max_temperature_c",
                           limit_value=acc_high, observed=peak_v, unit="degC",
                           citations=citations[:2])
                elif need is not None:
                    # 逐传感器保温时长
                    sres = next(s for s in zone_res.sensors if s.sensor_id == sid)
                    if sres.longest_in_band_seconds + 1e-6 < need:
                        citations = []
                        if series is not None:
                            citations += cite(series, st["first_in"],
                                              {"before": "band_enter_before",
                                               "after": "band_enter_after", "at": "band_enter"})
                            citations += cite(series, st["last_in"],
                                              {"before": "band_left_before",
                                               "after": "band_left_after", "at": "band_left"})
                        fc.add(code="SOAK_TOO_SHORT", severity=Severity.ERROR,
                               message=f"保温阶段 {stage.stage_id} 传感器 {sid} 最长连续在带 "
                                       f"{sres.longest_in_band_seconds:.0f}s < 要求 {need:.0f}s",
                               stage_id=stage.stage_id, zone_id=zone.zone_id, sensor_id=sid,
                               first_at=st["last_in"] or b, metric="longest_in_band_s",
                               limit_value=need, observed=sres.longest_in_band_seconds,
                               unit="s", citations=citations[:4])

            # 炉区整体（各区所有测点同时在带）保温时长
            if need is not None and zone_res.longest_simultaneous_in_band_seconds + 1e-6 < need:
                fc.add(code="SOAK_TOO_SHORT", severity=Severity.ERROR,
                       message=f"保温阶段 {stage.stage_id} 炉区 {zone.zone_id} 各测点同时在带 "
                               f"{zone_res.longest_simultaneous_in_band_seconds:.0f}s "
                               f"< 要求 {need:.0f}s",
                       stage_id=stage.stage_id, zone_id=zone.zone_id,
                       first_at=b, metric="zone_simultaneous_in_band_s",
                       limit_value=need,
                       observed=zone_res.longest_simultaneous_in_band_seconds, unit="s")

    return zone_res


# ---------- 入口 ----------


def evaluate(req) -> EvaluationReport:
    rule_version = req.rule_version or DEFAULT_RULE_VERSION
    if rule_version not in RULE_VERSIONS:
        raise ValueError(f"未知规则版本 {rule_version}，可选：{sorted(RULE_VERSIONS)}")
    rule_meta = RULE_VERSIONS[rule_version]

    fc = FindingCollector()
    pre = _precheck(req, fc)

    if pre.fatal:
        findings = fc.all()
        return EvaluationReport(
            batch_no=req.batch_no, furnace_id=req.furnace_id,
            recipe_id=req.recipe_id, recipe_version=req.recipe_version,
            rule_version=rule_version, status="INPUT_ERROR",
            first_violation=_first_error(findings), findings=findings, stages=[],
            input_summary=_summary(req, pre), evaluated_at=datetime.now(timezone.utc))

    max_gap = (pre.nominal_gap * req.offline_gap_factor
               if pre.nominal_gap is not None else float("inf"))

    stages = sorted(req.stages, key=lambda s: ep(s.start))
    stage_results: list[StageResult] = []
    for stage in stages:
        sr = StageResult(
            stage_id=stage.stage_id, name=stage.name, kind=stage.kind,
            start=as_utc(stage.start), end=as_utc(stage.end),
            target_low_c=stage.target_low_c, target_high_c=stage.target_high_c)
        for zone in req.zones:
            sr.zones.append(_evaluate_stage(stage, zone, pre.series_map, req,
                                            max_gap, fc, rule_meta))
        stage_results.append(sr)

    findings = fc.all()
    has_error = any(f.severity == Severity.ERROR for f in findings)
    return EvaluationReport(
        batch_no=req.batch_no, furnace_id=req.furnace_id,
        recipe_id=req.recipe_id, recipe_version=req.recipe_version,
        rule_version=rule_version, status="FAIL" if has_error else "PASS",
        first_violation=_first_error(findings), findings=findings,
        stages=stage_results, input_summary=_summary(req, pre),
        evaluated_at=datetime.now(timezone.utc))


def _first_error(findings: list[Finding]) -> Optional[Finding]:
    for f in findings:
        if f.severity == Severity.ERROR:
            return f
    return None


def _summary(req, pre: PrecheckResult) -> dict:
    per_sensor = {}
    units = set()
    t_min, t_max = None, None
    for r in req.readings:
        per_sensor[r.sensor_id] = per_sensor.get(r.sensor_id, 0) + 1
        units.add(r.unit.value)
        t = ep(as_utc(r.t))
        t_min = t if t_min is None else min(t_min, t)
        t_max = t if t_max is None else max(t_max, t)
    return {
        "batch_no": req.batch_no,
        "furnace_id": req.furnace_id,
        "recipe_id": req.recipe_id,
        "recipe_version": req.recipe_version,
        "zones": [{"zone_id": z.zone_id, "sensor_ids": z.sensor_ids,
                   "max_uniformity_delta": z.max_uniformity_delta} for z in req.zones],
        "stages": [{"stage_id": s.stage_id, "kind": s.kind.value,
                    "start": as_utc(s.start).isoformat(), "end": as_utc(s.end).isoformat(),
                    "target_band_c": [s.target_low_c, s.target_high_c]} for s in
                   sorted(req.stages, key=lambda x: ep(x.start))],
        "readings_total": len(req.readings),
        "readings_per_sensor": per_sensor,
        "reading_time_range": [dt_of(t_min).isoformat() if t_min is not None else None,
                               dt_of(t_max).isoformat() if t_max is not None else None],
        "units_seen": sorted(units),
        "nominal_sample_seconds": round(pre.nominal_gap, 3) if pre.nominal_gap else None,
        "params": {
            "max_heat_rate_c_min": req.max_heat_rate_c_min,
            "max_cool_rate_c_min": req.max_cool_rate_c_min,
            "max_overshoot_c": req.max_overshoot_c,
            "sensor_tolerance_c": req.sensor_tolerance_c,
            "max_sample_jitter": req.max_sample_jitter,
            "offline_gap_factor": req.offline_gap_factor,
            "offline_error_factor": req.offline_error_factor,
        },
    }
