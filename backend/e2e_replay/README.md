# 真实响应录制回放（独立 runner）

> **定位**：把 Step 5 留下的 3 份**真实** `/trip/generate` 响应存成 fixture，
> 离线用 `Itinerary.model_validate` 回放，验证解析器对真实输出的兼容性。
> 与 `backend/e2e_resilience/` 平级：那边验「外部服务坏了怎么降级」，这边验「真实输出还认不认」。
> 不进 `backend/tests/` 的 pytest 收集、不进 CI 默认门禁、不重跑模型、零费用、零网络。

## 跑法（PowerShell）

```powershell
& "D:\zhilv-yuntu\backend\.venv\Scripts\python.exe" "D:\zhilv-yuntu\backend\e2e_replay\replay.py"
```

屏幕只打 ASCII；中文报告由脚本写 UTF-8 的 `report.md`。退出码 `0` = 3 份全过，`1` = 有断言失败。

## 目录

| 文件 | 说明 |
|---|---|
| `fixtures/dali_3d.json` / `xiamen_2d.json` / `chengdu_3d.json` | 3 份真实响应（2026-11-14 Step 5 在线评估产出），各自 <20KB |
| `fixtures/_meta.json` | 每条的来源批次、目的地、预期天数 |
| `replay.py` | 加载 → Pydantic 校验 → 10 项断言 → 写报告 |
| `report.md` | 运行产物，重跑会刷新（含生成时间） |

**唯一脱敏动作**：把 `trip_id`（`trip_大理_2026-11-20` 这类一次性 ID）换成稳定的
`replay_<id>`，其余字段**逐字保留**，保证「原样回放」的口径成立。原件仍在
`backend/scripts/_local/generation_online_cases.json`（已被 `.gitignore` 忽略）。

## 10 项断言（每份样本都跑一遍）

1. `Itinerary.model_validate(raw)` 通过
2. 解析后 9 个顶层字段仍齐全
3. 解析后天数未变
4. 原始 9 个顶层字段都在
5. 天数与请求一致
6. `day_index` 连续 `1..N`
7. 预算 5 分项之和 == `budget_breakdown.total`
8. `total == estimated_budget`
9. `token_usage` 8 字段是 `int` 且 >= 0
10. `tips` / `source_notes` 是 `list[str]`

**观察项（不作断言）**：tips 是否含天气词。成都样本含「雨具」，是那批真实输出的事实，
Step 5 的 `weather_tip_ok 2/3` 已如实留档；回放不调阈值掩盖，也不把它写成失败。

## 口径与禁语

- 这 3 份是 **2026-11-14 的历史快照**，结论只能是「验证了解析器对这批真实输出的兼容性」，
  **不能**写成「验证了模型当前行为」「回放等于重新跑模型」。
- 与 Mock 的区别：Mock 是「先写好期望再照着造」，回放是「真实输出原样存下来验解析器」；
  前者可能自欺，后者能抓住模型多给字段 / 给 `null` / 改字段名这类形状变化。
- 脚本只 import `app.models.schemas`，import 前清空密钥、封死 `httpx` 与 `socket`（后者计数），
  不启动服务、不发请求；报告里记录外部 TCP 尝试次数。
- 禁语：「验证了真实模型当前行为」「回放覆盖了模型质量」「等价于线上回归整套」
  「这 3 份代表所有城市」。准确说法是「用 3 份真实响应做离线解析器兼容性回放」。

## 简历可用的一句

```
把 3 份真实模型响应脱敏存为 fixture 做离线回放，用 Pydantic 校验解析器兼容性
（字段完整性、天数连续、预算分项自洽、token 结构），零费用零网络、不进 CI 门禁。
```
