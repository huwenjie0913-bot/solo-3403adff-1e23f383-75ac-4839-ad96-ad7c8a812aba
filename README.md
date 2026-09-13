# 热处理炉批次曲线校核 API

热处理炉批次验收时，若只看几支热电偶曲线的**平均值**，三类问题会被掩盖：

- **短时超温**——单点冲高被相邻读数平均；
- **保温不足**——个别测点进出保温带时刻不同；
- **测点掉线**——空洞区间被平均值静默插值。

本服务对每支热电偶、每个炉区**逐测点逐阶段**校核，指出**第一处越界**及其相关传感器，
并给每条判定附上所引用的原始读数。

## 校核内容

预检（时序 / 量纲 / 采样间距）：

| 代码 | 含义 |
|---|---|
| `UNKNOWN_SENSOR` | 读数引用了未在炉区登记的传感器 |
| `DUPLICATE_SAMPLE` / `READINGS_NOT_ORDERED` | 重复采样时刻 / 时序未严格递增 |
| `STAGE_OVERLAP` | 材料阶段时间窗重叠 |
| `IMPLAUSIBLE_TEMPERATURE` | 超出物理可信范围 [-100, 2000]°C（量纲/坏点），剔除该点并判废 |
| `SAMPLE_GAP` | 采样间距 > 标称间隔 × `max_sample_jitter`（warning） |
| `SENSOR_OFFLINE` | 间距 > `offline_gap_factor` 倍（warning）；> `offline_error_factor` 倍判 error；掉线区间**不插值、不计保温** |

阶段校核（每阶段 × 每炉区 × 每测点）：

- **速率**：窗口内逐段计算 °C/min，`HEAT_RATE_EXCEEDED` / `COOL_RATE_EXCEEDED`（含保温阶段内的异常回升/下跌）；
- **进入/离开保温带时刻**、累计与最长连续在带时长：`NEVER_ENTERED_BAND` / `SOAK_TOO_SHORT`；
- **炉区均匀性**：合并各测点采样时刻为统一时间轴，两两温差求根切分，`NONUNIFORM`；
- **超温累计时长**：区分“超调区”（统计不判废）与越过硬界（`OVERTEMP`）。

### 容差口径（重要）

设保温带 `[L, H]`、允许超调 `O`、传感器容差 `t`：

- 验收有效保温带 `[L−t, H+t]`（读数在此带内时真实温度有可能合格）；
- 超温硬界（规则 **1.1.0**，当前默认）`H+O−t`——读数越过该界时真实温度**必然**超 `H+O`；
- 均匀性有效限值 = 限值 + `t`（限值按两测点真实温差定义，读数温差最多偏大一个容差；
  未给炉区限值时缺省取 `2t`，验收界即 `3t`）；
- 规则 **1.0.0** 的硬界为 `H+O`（不扣容差）。用 `/rules` 查看版本，请求体可显式指定
  `rule_version` 做旧规复核；规则版本随判定结果一并落库。

状态：`PASS`（无 error）/ `FAIL`（有工艺越界）/ `INPUT_ERROR`（输入与时序致命错误，仍落库可追溯）。
`first_violation` 为按时刻排序的第一条 error 判定。

## 接口

| 方法/路径 | 说明 |
|---|---|
| `POST /evaluate` | 提交校核，201 返回报告并写入 SQLite（结构错误 422） |
| `GET /evaluations/{id}` | 取单条校核 |
| `GET /batches/{batch_no}/evaluations` | 按批次号检索全部校核（重审历史） |
| `GET /batches/{batch_no}/latest` | 该批次最新一条 |
| `GET /compare?a={id}&b={id}` | 两条判定**并列查看**（b 缺省取同批次上一条）；`added/removed/changed` 按判定签名 `(code,stage,zone,sensor)` 比对 |
| `GET /rules` | 规则版本目录 |

并列查看同时服务于两种场景：**批次重审**（同批次多次提交对比）与
**配方版次并列**（同批次按不同 `recipe_version`/`rule_version` 校核后对比）。

每条 `finding` 的 `citations` 给出引用读数：采样时刻、传感器、提交值与原量纲、
摄氏度换算值，以及在判定中的角色（`before/after`、`peak`、`gap_left/gap_right`、
`band_enter/band_left`、`rate_start/rate_end`、`spread_high/spread_low` 等）。

## 运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# 数据库默认 ./data/curve_audit.db，可用环境变量 CURVE_DB_PATH 覆盖
```

示例请求：`examples/request_demo.json`（故意只含保温段末尾几个读数，其中一支热电偶
用华氏度 1652°F 提交以展示量纲换算——预期返回 201 + `FAIL`，含速率、超温、保温不足、
阶段无数据等多条判定；完整三段曲线的构造方式见 `tests/factory.py`）。

## 测试

```bash
python -m pytest tests/ -q
```

18 个用例覆盖：合格曲线、短时超温的两版规则差异、第一越界排序、降温速率、保温不足、
掉线 warning/error 分级、华氏度换算、坏点剔除不影响同区其他测点、双炉区独立判定、
落库检索、重审对比、配方版次并列、跨批次对比拒绝等。

## 实现说明

- 读数先按量纲换算为摄氏度并统一 UTC；线性插值只用于掉线阈值内的连续区间；
- 阶段窗口内以所有测点采样时刻为公共时间轴，对保温带边界、超温硬界、测点两两温差做
  **解析求根切分**，因此进/出带时刻、超温/在带/均匀累计时长是连续量而非逐点计数；
- 存储表 `evaluations` 保存规则版本、输入摘要（`input_summary`）、原始请求 JSON 与
  完整判定报告 JSON，按 `batch_no` 建索引。
