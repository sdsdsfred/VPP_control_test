# VPP 输入输出接口字段文档

本文档基于当前代码实现，描述 REST API 与 Socket.IO 实时事件字段。

## 1. 总览

- 服务入口: `python web/app.py`
- 默认基地址: `http://127.0.0.1:8000`
- 实时事件通道: Socket.IO 事件名 `update`

## 2. REST API

### 2.1 POST /api/input/scenario

用途: 提交仿真输入场景（预测 + 实际）。

请求体字段:

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| pv_forecast_min | number[] | 是 | 光伏预测下界，长度=hours |
| pv_forecast_max | number[] | 是 | 光伏预测上界，长度=hours |
| load_forecast_min | number[] | 是 | 负荷预测下界，长度=hours |
| load_forecast_max | number[] | 是 | 负荷预测上界，长度=hours |
| actual_loads | number[][] | 是 | 实际负荷，形状=(hours, n_load_groups) |
| market_daily_cap | number | 否 | 场景交易上限；不传时使用 `sum(load_forecast_max)` |

请求示例:

```json
{
  "pv_forecast_min": [120, 100, 90],
  "pv_forecast_max": [180, 150, 130],
  "load_forecast_min": [800, 820, 780],
  "load_forecast_max": [980, 1010, 960],
  "actual_loads": [
    [200, 190, 210, 180],
    [205, 195, 200, 185],
    [198, 188, 202, 181]
  ],
  "market_daily_cap": 2600
}
```

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| hours | number | 小时数 |
| n_load_groups | number | 负荷组数 |
| market_daily_cap | number | 生效的 cap |

失败响应:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | false |
| error | string | 错误信息 |

实现约束（服务端自动处理）:

- 每小时总实际负荷会被限制在预测中值的 ±20%。
- 每日总实际负荷会被抬升到不低于每日预测中值总量的 98%。

---

### 2.2 GET /api/input/scenario

用途: 查询是否已加载输入场景。

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| has_scenario | boolean | 是否已有场景 |
| hours | number | 仅 has_scenario=true 时返回 |
| n_load_groups | number | 仅 has_scenario=true 时返回 |
| market_daily_cap | number | 仅 has_scenario=true 时返回 |

---

### 2.3 POST /api/simulation/start

用途: 启动仿真。

请求体字段:

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| seed | number | 否 | 1 | 随机种子 |
| delay_seconds | number | 否 | 2.0 | 每小时事件间隔（秒） |
| use_injected | boolean | 否 | true | 是否优先使用已注入场景 |

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| started | boolean | 是否已启动 |
| source | string | `injected` 或 `generated` |

错误:

- 409: 仿真已在运行。

---

### 2.4 GET /api/simulation/status

用途: 查询仿真状态。

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| running | boolean | 仿真是否运行中 |
| has_injected_scenario | boolean | 是否存在注入场景 |

---

### 2.5 GET /api/scenario/current-cap

用途: 查询当前运行场景的 market_daily_cap。

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| running | boolean | 仿真是否运行中 |
| has_cap | boolean | 当前是否可返回 cap |
| market_daily_cap | number/null | 当前 cap |

---

### 2.6 GET /api/output/dispatch/latest

用途: 获取最新一条调度输出。

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| has_data | boolean | 是否有数据 |
| data | object/null | 最新调度记录 |

`data` 字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| hour | number | 小时索引 |
| load_targets | number[] | 负荷目标 |
| storage_target | number | 储能目标（+充电/-放电） |
| control_status | string | 优化状态 |
| adjust_reason | string | 调整原因 |

---

### 2.7 GET /api/output/dispatch/history?limit=24

用途: 获取调度历史。

查询参数:

| 参数 | 类型 | 默认 | 范围 |
|---|---|---|---|
| limit | number | 24 | 1~200 |

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| count | number | 返回条数 |
| items | object[] | 调度记录数组（字段同 dispatch/latest 的 data） |

---

### 2.8 GET /api/output/correction/latest

用途: 获取最新负荷修正策略。

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| has_data | boolean | 是否有策略 |
| data | object/null | 策略对象 |

`data` 字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| hour | number | 触发小时 |
| reduction_ratio | number | 回调比例 |
| target_hour_indices | number[] | 目标小时索引 |
| target_hour_loads | number[] | 目标小时负荷 |

---

### 2.9 GET /api/output/storage/latest

用途: 获取最新储能状态与调控策略。

响应字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| ok | boolean | 是否成功 |
| has_data | boolean | 是否有数据 |
| data | object/null | 储能状态对象 |

`data` 字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| hour | number | 小时索引 |
| storage_soc | number | 储能 SoC（%） |
| storage_target | number | 储能目标（+充电/-放电） |
| storage_priority | string | `storage` / `load` / `none` |
| stored_diff | number | 充电差值 |
| excess_diff | number | 放电超额 |
| adjust_reason | string | 调整原因 |

## 3. Socket.IO 实时事件

### 3.1 事件名

- `update`

### 3.2 每小时事件字段

| 字段 | 类型 | 说明 |
|---|---|---|
| hour | number | 小时索引 |
| pv_min | number | 光伏预测下界 |
| pv_max | number | 光伏预测上界 |
| load_forecast_min | number | 负荷预测下界 |
| load_forecast_max | number | 负荷预测上界 |
| actual_total | number | 物理实际总负荷 |
| achieved_total | number | 调控后达成总负荷 |
| control_status | string | 优化状态 |
| adjust_reason | string | 调整原因 |
| load_targets | number[] | 负荷目标 |
| storage_target | number | 储能目标 |
| market_daily_cap | number | 场景 cap |
| total_energy_consumed | number | 全程累计实际能耗 |
| energy_procured_total | number | 全仿真周期购电总量 |
| energy_procured_today | number | 当前自然日购电预算 |
| energy_remaining_today | number | 当前自然日剩余购电量 |
| cumulative_forecast_mid | number | 截至当前小时累计预测中值 |
| cumulative_overuse | number | 截至当前小时累计超用 |
| correction_strategy | object | 修正策略 |
| storage_soc | number | SoC |
| storage_priority | string | 储能调控优先级 |
| stored_diff | number | 充电差值 |
| excess_diff | number | 放电超额 |

### 3.3 日切片事件字段（day start/replay）

以下字段在 `hour % 24 == 0` 或客户端重连回放时出现:

| 字段 | 类型 | 说明 |
|---|---|---|
| day_index | number | 第几天（从 1 开始） |
| day_load_min | number[] | 当天 24h 负荷下界 |
| day_load_max | number[] | 当天 24h 负荷上界 |
| day_pv_mid | number[] | 当天 24h 光伏中值 |

## 4. 字段单位与约定

- 负荷/光伏/储能目标: kW
- 能量累计字段: 文档中按小时步长累计，前端显示常用 kWh 语义
- `storage_target` 正值表示充电，负值表示放电

## 5. 联调建议

- 想看完整多日字段，建议 `hours=72`。
- 前端实时节奏默认每小时 2 秒，可通过 `delay_seconds` 调整。
- 当接口无数据时，统一返回 `has_data=false` + `data=null`。
