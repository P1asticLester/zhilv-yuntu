# 故障注入演练报告

> 生成时间：2026-11-15 16:47:20　脚本：`backend/e2e_resilience/run_drills.py`　结果：**3/3 PASS**

## 一句话结论

主动把外部依赖弄坏 3 次，`/trip/generate` 都返回 **200**、3 天行程完整、关键字段没丢，降级发生在各自不同的代码位置；全程零真实网络调用。

## 逐场景结果

| 场景 | 注入点 | 状态码 | 天数 | 降级发生位置 | 结果 |
|---|---|---|---|---|---|
| LLM 调用超时 | `app.agents.trip_planner_agent._build_chat_llm -> TimeoutError on invoke()` | 200 | 3 | trip_planner_agent.py:266-275 捕获异常，generate_planner_draft 返回 None | PASS |
| 高德 429 限流 | `app.services.trip_service.enrich_itinerary_with_map_data -> AmapServiceError` | 200 | 3 | trip_service.py:240-244 吞掉异常，继续做预算回算 | PASS |
| 模型返回坏 JSON | `app.agents.trip_planner_agent._build_chat_llm -> content 无法提取 JSON` | 200 | 3 | trip_planner_agent.py:287-291 提取 JSON 失败，generate_planner_draft 返回 None | PASS |

### 每条用例实际断言了什么

**LLM 调用超时**

- PASS　不返回 5xx（状态码 200）
- PASS　天数不空且为 3 天
- PASS　budget_breakdown 字段在
- PASS　token_usage 字段在
- PASS　tips 非空
- PASS　source_notes 非空
- PASS　每天结构完整（spots / meals / hotel / transport）
- PASS　没走模型成功路径（planner token 为 0）

- `planner_prompt_tokens` = `0`
- 实际 `source_notes` 首条：`Itinerary is assembled by trip_service.py and can optionally use LangChain structured output.`

**高德 429 限流**

- PASS　不返回 5xx（状态码 200）
- PASS　天数不空且为 3 天
- PASS　budget_breakdown 字段在
- PASS　token_usage 字段在
- PASS　tips 非空
- PASS　source_notes 非空
- PASS　每天结构完整（spots / meals / hotel / transport）
- PASS　没走模型成功路径（planner token 为 0）

- `planner_prompt_tokens` = `0`
- 实际 `source_notes` 首条：`Itinerary is assembled by trip_service.py and can optionally use LangChain structured output.`

**模型返回坏 JSON**

- PASS　不返回 5xx（状态码 200）
- PASS　天数不空且为 3 天
- PASS　budget_breakdown 字段在
- PASS　token_usage 字段在
- PASS　tips 非空
- PASS　source_notes 非空
- PASS　每天结构完整（spots / meals / hotel / transport）
- PASS　没走模型成功路径（planner token 为 0）

- `planner_prompt_tokens` = `0`
- 实际 `source_notes` 首条：`Itinerary is assembled by trip_service.py and can optionally use LangChain structured output.`

### 降级证据（从产品日志里抓到的原文）

**LLM 调用超时**

- `[trip_planner_agent] 大模型调用失败: TimeoutError: drill: simulated LLM timeout`
- `[rerank] skip qwen3-rerank: missing LLM_API_KEY or empty chunks`
- `[rerank] qwen3-rerank unavailable, using rule-based rerank`

**高德 429 限流**

- `[rerank] skip qwen3-rerank: missing LLM_API_KEY or empty chunks`
- `[rerank] qwen3-rerank unavailable, using rule-based rerank`

**模型返回坏 JSON**

- `[trip_planner_agent] 未能从模型返回中提取 JSON。`
- `[trip_planner_agent] 原始返回预览: 这不是 JSON，也不是代码块，只是一段普通文字。`
- `[rerank] skip qwen3-rerank: missing LLM_API_KEY or empty chunks`
- `[rerank] qwen3-rerank unavailable, using rule-based rerank`

> 场景「高德 429 限流」没有地图异常对应的日志，是**预期行为**：`trip_service.py:243` 的 `except Exception: pass` 会静默吞掉地图异常，所以只能靠「状态码 200 + 结构完整 + 字段不丢」来证明降级生效。

## 环境与口径（别被问穿）

- 外部 TCP 连接尝试：**0 次**。脚本在 import 产品代码前清空密钥、封死 `httpx.Client.request` 与 `socket.create_connection`，并把 Chroma 固定为本地关键词检索；**零费用、零真实网络**。
- LLM 全程保持「无密钥」状态；场景 1/3 是 patch `_build_chat_llm` 直接返回假对象，不是伪造 key 去连真模型。
- 三个场景都走 6 城 **CURATED 路径**（目的地「大理」）。该路径的降级是「`llm_draft is None` → 服务层用规则组装行程」，`source_notes` 里**没有**「规则方案」字样；那句话只出现在 DYNAMIC 路径（`trip_service.py:493/495`），别拿它当断言。
- **错误码分级不在这份报告里**：422（用户输入）/ 503（城市解析、候选池不可用）属于路由层，由 `backend/tests/test_request_boundaries.py` 的 8 条接口契约测试覆盖；本报告只验「外部服务坏了还能不能给出可用行程」。
- 本项目本来就有降级代码，这份演练**不是修 bug、不是加功能**，加的是「我造过故障、逐条看到结果」的可展示证据。
- 独立 runner：不进 `backend/tests/` 的 pytest 收集，不进 CI 默认门禁，不改任何产品代码；`patch` 只在 `with` 块内生效，退出自动还原。

## 复现（PowerShell）

```powershell
& "D:\zhilv-yuntu\backend\.venv\Scripts\python.exe" "D:\zhilv-yuntu\backend\e2e_resilience\run_drills.py"
```

禁语：「验证了真实模型超时」「覆盖全部降级分支」「做了完整故障演练体系」。
