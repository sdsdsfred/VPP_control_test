"""
Web前端储能状态同步说明

前端现已支持实时显示：

1. 储能状态卡片（新增）
   ┌─ 储能状态 ────────────────────┐
   │ 电池状态 (SoC)    45.2 %      │
   │                                │
   │ 本小时调控                    │
   │ ⚡ 放电 -39.5 kW             │
   │ 差值: 127.4 kW (放)          │
   │                                │
   │ 优先级       储能优先         │
   └────────────────────────────────┘

2. 实时数据来源
   后端 → Socket.IO事件 → 前端JS处理 → UI更新
   
   事件字段映射：
   ├─ storage_soc         → SoC显示（% 形式）
   ├─ storage_target      → 充放电动作（⚡符号标记）
   ├─ storage_priority    → 优先级标记
   ├─ stored_diff         → 充电差值
   └─ excess_diff         → 放电超额

3. 动作颜色编码
   • 充电 (+) = 绿色 (#27ae60)  ⚡ 充电 +25.0 kW
   • 放电 (-) = 红色 (#e74c3c)  ⚡ 放电 -39.5 kW
   • 无动作   = 灰色 (#7f8c8d)  -- 无动作

4. 日志示例
   H5 • 实际 874.6 kW → 达成 874.6 kW • RCA=discharge_storage | 储能=storage

5. Clear按钮
   - 重置所有数据和图表
   - 保留市场电量上限
   - 重置存储显示为初始状态

文件清单：
- web/static/index.html       (已更新，+新储能卡片, +JS处理)
- web/app.py                 (已更新，+/api/output/storage/latest端点)
- main.py                    (已更新，+存储事件字段)
- vpp/rca.py                 (已更新，+储能决策逻辑)
- README.md                  (已更新，+储能策略说明)
"""
print(__doc__)
