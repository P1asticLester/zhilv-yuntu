# 故障注入演练（独立 runner）

> **定位**：主动制造外部依赖故障，逐条观察 `/trip/generate` 的降级行为，产出可当场展示的报告。
> 和 `frontend/e2e/` 的 UI 冒烟是同一套方法论：**造一个可控输入 → 看系统反应 → 留一份产物**。
> 不改产品代码、不进 `backend/tests/` 的 pytest 收集、不进 CI 默认门禁、零费用、零真实网络。

## 跑法（PowerShell）

```powershell
& "D:\zhilv-yuntu\backend\.venv\Scripts\python.exe" "D:\zhilv-yuntu\backend\e2e_resilience\run_drills.py"
```

屏幕只打 ASCII（PowerShell 5.1 中文管道会乱码），中文报告由脚本自己写 UTF-8 的 `report.md`。
退出码 `0` = 3 条全过，`1` = 有断言失败。

## 三个场景

| # | 造什么故障 | patch 哪个函数 | 降级发生位置 | 期望 |
|---|---|---|---|---|
| 1 | LLM 调用超时 | `trip_planner_agent._build_chat_llm` → `invoke()` 抛 `TimeoutError` | `trip_planner_agent.py:266-275` 捕获异常，草稿返回 `None` | 200 / 3 天 / 字段不丢 |
| 2 | 高德 429 限流 | `trip_service.enrich_itinerary_with_map_data` → 抛 `AmapServiceError` | `trip_service.py:240-244` 吞掉异常，继续预算回算 | 200 / 3 天 / 字段不丢 |
| 3 | 模型返回坏 JSON | `trip_planner_agent._build_chat_llm` → `content` 提取不出 JSON | `trip_planner_agent.py:287-291` 草稿返回 `None` | 200 / 3 天 / 字段不丢 |

每条 8 项断言：① 不是 5xx ② 天数 3 天 ③ `budget_breakdown` 在 ④ `token_usage` 在
⑤ `tips` 非空 ⑥ `source_notes` 非空 ⑦ 每天 `spots/meals/hotel/transport` 结构完整
⑧ `planner_prompt_tokens == 0`（确实没走模型成功路径）。

**场景 2 没有降级日志是预期行为**：`except Exception: pass` 会静默吞掉地图异常，
所以只能靠「结构完整 + 字段不丢」证明降级生效——这一点已写进 `report.md`。

## 为什么脚本自带离线引导

`backend/tests/conftest.py` 的 autouse fixture 只在 **pytest 进程**里生效，独立脚本拿不到。
所以 `run_drills.py` 在 import 产品代码之前自己：

- 清空 `LLM_API_KEY` / `AMAP_API_KEY`，关 Redis，设 `LLM_MAX_RETRIES=0`；
- 设 `ENABLE_AMAP_ENRICHMENT=true`，让场景 2 能进入 `try` 块；
- 封死 `httpx.Client.request` / `AsyncClient.request` 与 `socket.create_connection`（后者同时计数）；
- 把 Chroma 固定为本地关键词检索。

LLM 全程**无密钥**；场景 1/3 是 patch `_build_chat_llm` 直接返回假对象，
不是伪造 key 去连真模型。`patch` 只在 `with` 块内生效，退出自动还原。

## 口径与禁语

- 三个场景都走 6 城 **CURATED 路径**（「大理」）。该路径降级是
  「`llm_draft is None` → 服务层规则组装」，`source_notes` 里**没有**「规则方案」字样；
  那句话只在 DYNAMIC 路径（`trip_service.py:493/495`），别拿它当断言。
- **错误码分级不在这份报告里**：422 / 503 属路由层，由
  `backend/tests/test_request_boundaries.py` 的 8 条接口契约测试覆盖。
- 本项目本来就有降级代码，这份演练**不是修 bug、不是加功能**，加的是可展示证据。
- 禁语：「验证了真实模型超时」「覆盖全部降级分支」「做了完整故障演练体系」
  「这 3 条等价于线上混沌工程」。准确说法是「补了 3 条离线故障注入演练」。

## 简历可用的一句

```
补 3 条离线故障注入演练（模型超时 / 地图限流 / 非法 JSON），验证降级路径不返回 5xx、
行程天数与关键字段不丢，产出可复现的演练报告；不进 CI 门禁、零真实网络与模型费用。
```
