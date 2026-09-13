"""校核规则版本。

规则语义说明（容差如何参与温度判定）
----------------------------------
每条传感器读数带计量容差 sensor_tolerance_c（t），目标保温带为 [L, H]，
允许超调为 O。对某一时刻真实温度 T，读数可能落在 [T-t, T+t]：

* 验收有效带（acceptance band）：[L-t, H+t]
  读数在此带内时，真实温度“有可能”合格。进入/离开保温带时刻、保温时长
  与区间均匀性均按该带判定，保证验收不冤枉好批次。
* 超温硬界（overtemp ceiling）：H + O - t
  读数超过该界时，真实温度“必然”超过 H+O（即使减去容差仍超），判
  OVERTEMP；读数位于 H+t 与 H+O-t 之间属于超调区，只累计统计不判废。
* 均匀性限值按“两测点真实温差”定义；读数误差使读数温差最多偏大一个
  容差 t，验收有效限值 = 限值 + t（缺省限值 2t，验收界即 3t）。

版本历史
~~~~~~~~
1.0.0  初版：容差只放宽保温带与均匀性；超温硬界为 H+O（不扣容差），
       超温累计按读数 > H+O 计。
1.1.0  当前版：超温硬界改为 H+O-t（计量必然超温才判废），避免平均值
       之外的单点误差被容差放大；新增规则 SENSOR_STAGE_NO_DATA。
"""
from __future__ import annotations

RULE_VERSIONS: dict[str, dict] = {
    "1.0.0": {
        "label": "初版",
        "overshoot_tolerance_applied": False,
        "changelog": "容差放宽保温带与均匀性；超温硬界 = H + overshoot。",
    },
    "1.1.0": {
        "label": "当前版",
        "overshoot_tolerance_applied": True,
        "changelog": "超温硬界 = H + overshoot - tolerance（必然超温才判废）。",
    },
}

DEFAULT_RULE_VERSION = "1.1.0"

# 校核判定代码
CODES = {
    # 输入与时序类（INPUT_ERROR 致命）
    "UNKNOWN_SENSOR": "读数引用了未在炉区登记的传感器",
    "DUPLICATE_ZONE_ID": "炉区编号重复",
    "SENSOR_IN_MULTIPLE_ZONES": "同一传感器登记在多个炉区",
    "DUPLICATE_STAGE_ID": "阶段编号重复",
    "DUPLICATE_SAMPLE": "同一传感器存在重复采样时刻",
    "READINGS_NOT_ORDERED": "读数未按采样时刻严格递增",
    "STAGE_OVERLAP": "材料阶段时间窗重叠",
    "IMPLAUSIBLE_TEMPERATURE": "温度超出物理可信范围",
    # 采集质量类
    "SAMPLE_GAP": "采样间距超过允许倍数",
    "SENSOR_OFFLINE": "测点掉线：相邻读数间隔超过掉线阈值",
    "SENSOR_STAGE_NO_DATA": "阶段内缺少该测点有效读数，速率/保温无法校核",
    # 速率
    "HEAT_RATE_EXCEEDED": "升温速率超过上限",
    "COOL_RATE_EXCEEDED": "降温速率超过上限",
    # 保温带 / 超温 / 均匀性
    "SOAK_TOO_SHORT": "保温时长不足",
    "NEVER_ENTERED_BAND": "保温阶段从未进入目标保温带",
    "OVERTEMP": "短时超温：读数越过超调硬界",
    "NONUNIFORM": "炉区测点温差超过均匀性限值",
    "SOAK_BAND_REQUIRED": "保温阶段必须提供目标温度带",
}

# 物理可信范围（摄氏度），超出视为量纲/采集错误而非工艺超温
PHYSICAL_MIN_C = -100.0
PHYSICAL_MAX_C = 2000.0
