# VPP MILP 控制示例

这是一个最小可运行的虚拟电厂（VPP）控制示例，展示如何：

- 随机生成光伏与负荷预测上下限、每日可交易能量上限与实时实际负荷
- 使用 MILP（pulp）生成负荷调控与储能目标
- 接收实时执行反馈，做根因分析（RCA），并依据偏差点重新生成调控策略

快速开始（Windows PowerShell）：

```powershell
python -m pip install -r requirements.txt
python main.py
```

项目结构：
- `main.py`：运行仿真与控制循环的入口
- `vpp/simulator.py`：生成仿真输入数据与模拟实时执行
- `vpp/model.py`：构建并求解 MILP，输出控制目标
- `vpp/rca.py`：根因分析并生成调整建议（储能优先策略）
- `web/app.py`：Flask + Socket.IO 服务器，提供REST API与实时事件流
- `web/static/index.html`：Web前端，展示实时仿真数据
- `web/api_flow_example.py`：一键演示脚本
- `test_storage_priority.py`：储能优先调控策略演示脚本

该示例为教学用途，算法可按需扩展为更详细的设备模型、成本函数和约束。

## 储能优先调控策略

系统采用分层的储能与负荷调控策略，确保储能被优先利用以吸收负荷变化，只有在储能不足或无反馈时才触发负荷调控：

1. **实际负荷低于预测中值**
   - 优先动作：发送储能充电命令，将差值存储于电池
   - 条件：储能状态(SoC)< 90% 且储能可响应
   - 备选：若储能无反馈或SoC临界，则增大负荷以接近预测

2. **实际负荷高于预测中值**
   - 优先动作：发送储能放电命令，用储能补偿超出部分
   - 条件：储能状态(SoC) > 10% 且储能可响应
   - 备选：若储能无反馈或SoC过低，则触发负荷调控（缩减最大偏差组）

3. **储能响应超时或状态不良**
   - 在最近2小时内发送了储能命令但未得到执行反馈，则认为储能无响应
   - 或SoC < 20%（临界状态），触发动作降级到负荷调控

## 通过接口输入预测与实际执行信息

现在支持通过 Web API 注入仿真输入（预测信息 + 实际执行信息），再启动仿真。

1. 启动服务

```powershell
python web/app.py
```

2. 提交场景输入（POST `/api/input/scenario`）

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
	"market_daily_cap": 2600,
	"market_hourly_cap": [110, 120, 130]
}
```

字段说明：
- `pv_forecast_min/max`、`load_forecast_min/max`：长度都必须等于 `hours`。
- `actual_loads`：二维数组，形状为 `(hours, n_load_groups)`。
- `market_hourly_cap`：可选；长度必须等于 `hours`。表示 D-2 日确定的每小时购电约束（优先使用）。
- `market_daily_cap`：兼容字段；当未传 `market_hourly_cap` 时，系统按该值在每天 24h 内按预测中值比例拆分为每小时约束。

能耗总额机制（当前实现）：
- 系统按全天 `load_forecast_min/max` 的中值提前统计总购电量（`energy_procured_total`）。
- 同时按自然日（每 24 小时）拆分购电预算；每一天的预测总量不同，因此每天的购电量也不同。
- 实时执行时，若累计实际执行高于累计预测中值（出现 `cumulative_overuse`），系统会生成后续 2 小时的负荷回调策略。
- 回调比例计算：`reduction_ratio = cumulative_overuse / (后续2小时预测中值之和)`，并限制最大 30%。
- 例如后续 2 小时预测各 600 kW，当前累计超用 100 kW，则回调比例约为 `100/(600+600)=8.33%`。

多日差异设置（默认生成与示例脚本均生效）：
- 每天的预测负荷曲线有日级系数差异。
- 每天的光伏预测曲线有日级系数差异。
- 每天的实际执行负荷在预测基础上带有不同日偏置。

3. 通过接口启动仿真（POST `/api/simulation/start`）

```json
{
	"seed": 1,
	"delay_seconds": 2.0,
	"use_injected": true
}
```

默认 `use_injected=true`，会优先使用第 2 步提交的输入。

4. 查看状态
- GET `/api/input/scenario`：是否已加载输入场景。
- GET `/api/simulation/status`：仿真是否运行中。
- GET `/api/scenario/current-cap`：获取当前运行场景的 `market_daily_cap`。

5. 仿真前批量设置实时光伏时序（POST `/api/input/realtime-pv/schedule`）

请求示例：

```json
{
	"start_hour": 0,
	"values": [
		120.5,
		118.0,
		110.2,
		95.6
	]
}
```

说明：
- `values[i]` 会映射到 `hour = start_hour + i`。
- 该接口用于在仿真阶段前一次性下发时序数据，运行时按小时读取，不需要前端手动逐条输入。

成功响应示例：

```json
{
	"ok": true,
	"count": 4,
	"start_hour": 0,
	"end_hour": 3
}
```

查看当前时序配置（GET `/api/input/realtime-pv/schedule`）示例：

```json
{
	"ok": true,
	"has_data": true,
	"count": 72,
	"start_hour": 0,
	"end_hour": 71
}
```

可选：也可在场景接口中直接携带 `realtime_pv_schedule`（长度需等于 `hours`），系统会在加载场景时自动缓存该时序。

6. 获取调度输出（负荷目标 + 储能目标）
- GET `/api/output/dispatch/latest`：获取最新一条调度输出。
- GET `/api/output/dispatch/history?limit=24`：获取最近 N 条调度输出（默认 24，最大 200）。

返回数据示例：

```json
{
	"ok": true,
	"has_data": true,
	"data": {
		"hour": 5,
		"load_targets": [198.2, 185.7, 206.1, 179.3],
		"storage_target": 12.5,
		"control_status": "Optimal",
		"adjust_reason": "ok"
	}
}
```

7. 获取修正策略（能耗预算超用时的回调策略）
- GET `/api/output/correction/latest`：获取最新的负荷修正策略。

返回数据示例（当有超用时）：

```json
{
	"ok": true,
	"has_data": true,
	"data": {
		"hour": 5,
		"reduction_ratio": 0.0833,
		"target_hour_indices": [6, 7],
		"target_hour_loads": [566.0, 569.5]
	}
}
```

当无修正策略时（即当前无累计超用）：

```json
{
	"ok": true,
	"has_data": false,
	"data": null
}
```

8. 获取储能状态（储能充放电备策略）
- GET `/api/output/storage/latest`：获取最新的储能调控状态。

返回数据示例（当发送storage命令时）：

```json
{
	"ok": true,
	"has_data": true,
	"data": {
		"hour": 5,
		"storage_soc": 45.2,
		"storage_target": 15.5,
		"storage_priority": "storage",
		"stored_diff": 20.3,
		"adjust_reason": "charge_storage"
	}
}
```

字段说明：
- `storage_soc`：当前储能状态（0-100%）
- `storage_target`：本小时的储能调控目标（正数=充电，负数=放电，单位kW）
- `storage_priority`：调控优先级（"storage"=优先储能，"load"=降级到负荷调控，"none"=无调控）
- `stored_diff`：若为充电，差值量（实际低于预测的部分，单位kW）
- `excess_diff`：若为放电，超额量（实际高于预测的部分，单位kW）
- `adjust_reason`：调控原因（如"charge_storage"、"discharge_storage"、"reduce_group_0_no_discharge"等）

每小时事件还会附带以下能耗控制字段（用于前端/接口消费）：
- `energy_procured_total`：按预测中值统计的全仿真周期购电总量。
- `energy_procured_today`：当前自然日（24h）的购电预算。
- `energy_remaining_today`：当前自然日剩余购电量。
- `cumulative_forecast_mid`：截至当前小时的累计预测中值。
- `cumulative_overuse`：截至当前小时的累计超用量。
- `correction_strategy`：后续 2 小时回调策略，包含 `reduction_ratio` 与 `target_hour_loads`；也可通过 `/api/output/correction/latest` 接口单独获取。
- `storage_soc`：当前储能状态百分比。
- `storage_priority`：本小时采用的处理优先级。

9. 一键流程示例脚本（上传 -> 启动 -> 轮询）

```powershell
python web/api_flow_example.py --base-url http://127.0.0.1:8000
```

自动拉起服务（可选）：

```powershell
python web/api_flow_example.py --base-url http://127.0.0.1:8000 --auto-start-server
```

可选参数：
- `--hours`：示例输入时长（默认 24）
- `--groups`：负荷分组数（默认 4）
- `--delay-seconds`：仿真事件间隔（默认 2.0）
- `--poll-interval`：状态轮询间隔秒数（默认 1.0）
- `--timeout-seconds`：轮询超时秒数（默认 90）
- `--wait-server-seconds`：上传前等待服务可达的秒数（默认 0，不等待）
- `--payload-seed`：示例负荷数据随机种子（默认 42，用于 5% 随机波动复现）
- `--auto-start-server`：自动拉起 `web/app.py` 子进程（默认关闭）
- `--server-script`：自动拉起时的服务脚本路径（默认 `app.py`）
- `--keep-server-running`：流程结束后不关闭自动拉起的服务进程