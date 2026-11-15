# 真实响应录制回放报告

> 生成时间：2026-11-15 18:01:22　脚本：`backend/e2e_replay/replay.py`　结果：**3/3 PASS**

## 一句话结论

把 3 份**真实** `/trip/generate` 响应原样存成 fixture，离线用 `Itinerary.model_validate` 回放，10 项断言全过；以后每次改动都能拿这批真实输出验解析器兼容性。

## 逐样本结果

| 样本 | 目的地 | 采集批次 | 天数 | 预算 | token 合计 | 结果 |
|---|---|---|---|---|---|---|
| `dali_3d` | 大理 | 2026-11-14 | 3（预期 3） | 2206.72 | 6460 | PASS |
| `xiamen_2d` | 厦门 | 2026-11-14 | 2（预期 2） | 1257.5 | 6028 | PASS |
| `chengdu_3d` | 成都 | 2026-11-14 | 3（预期 3） | 3991.01 | 6424 | PASS |

### 每条样本实际断言了什么

**`dali_3d`**

- PASS　Itinerary.model_validate 通过
- PASS　解析后 9 个顶层字段仍齐全
- PASS　解析后天数未变
- PASS　原始 9 个顶层字段都在
- PASS　天数与请求一致
- PASS　day_index 连续 1..N
- PASS　预算 5 分项之和 == total
- PASS　total == estimated_budget
- PASS　token_usage 8 字段是 int 且 >= 0
- PASS　tips / source_notes 是 list[str]

- 观察项（**不作断言**）：tips 是否含天气词 = `False`

**`xiamen_2d`**

- PASS　Itinerary.model_validate 通过
- PASS　解析后 9 个顶层字段仍齐全
- PASS　解析后天数未变
- PASS　原始 9 个顶层字段都在
- PASS　天数与请求一致
- PASS　day_index 连续 1..N
- PASS　预算 5 分项之和 == total
- PASS　total == estimated_budget
- PASS　token_usage 8 字段是 int 且 >= 0
- PASS　tips / source_notes 是 list[str]

- 观察项（**不作断言**）：tips 是否含天气词 = `True`

**`chengdu_3d`**

- PASS　Itinerary.model_validate 通过
- PASS　解析后 9 个顶层字段仍齐全
- PASS　解析后天数未变
- PASS　原始 9 个顶层字段都在
- PASS　天数与请求一致
- PASS　day_index 连续 1..N
- PASS　预算 5 分项之和 == total
- PASS　total == estimated_budget
- PASS　token_usage 8 字段是 int 且 >= 0
- PASS　tips / source_notes 是 list[str]

- 观察项（**不作断言**）：tips 是否含天气词 = `True`

> 成都样本的 tips 含「雨具」，这是 2026-11-14 那批真实输出的事实，Step 5 的 `weather_tip_ok 2/3` 已如实留档；回放不调阈值掩盖，也不把它写成失败。

## 口径（别被问穿）

- 外部 TCP 连接尝试：**0 次**。脚本只 import `app.models.schemas` 做 Pydantic 校验，import 前清空密钥、封死 `httpx` 与 `socket`；**不启动服务、不重跑模型、零费用**。
- fixture 的唯一脱敏动作是把 `trip_id`（`trip_大理_2026-11-20` 这类一次性 ID）换成稳定的 `replay_<id>`，**其余字段逐字保留**，保证「原样回放」成立。
- 这 3 份是 **2026-11-14 Step 5 真模型在线评估的历史快照**，结论只能是「验证了解析器对这批真实输出的兼容性」，**不能**写成「验证了模型当前行为」「回放等于重新跑模型」。
- 与 Mock 的区别：Mock 是「先写好期望再照着造」，回放是「真实输出原样存下来验解析器」；前者可能自欺，后者能抓住真实的形状变化。
- 独立 runner：不进 `backend/tests/` 的 pytest 收集，不进 CI 默认门禁，不改产品代码。
- 与 `backend/e2e_resilience/` 平级：那边验「外部服务坏了怎么降级」，这边验「真实输出还认不认」，都是离线、有产物、能当场演示的轻量演练。

## 复现（PowerShell）

```powershell
& "D:\zhilv-yuntu\backend\.venv\Scripts\python.exe" "D:\zhilv-yuntu\backend\e2e_replay\replay.py"
```

禁语：「验证了真实模型当前行为」「回放覆盖了模型质量」「等价于线上回归整套」。
