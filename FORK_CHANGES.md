# 二次开发改动说明

> 本仓库 fork 自 [tutu-zzz/zhilv-yuntu](https://github.com/tutu-zzz/zhilv-yuntu) @ `73378d5`（2026-07-26，tag `upstream-base`）。上游全部提交历史、作者署名与文件内容完整保留；本文件只记录 fork 之后的 16 个提交，不主张原创。上游自身的迭代记录见 [CHANGELOG.md](./CHANGELOG.md)。

## 一、这轮改动解决什么

上游已经是一个功能完整的旅行规划系统。二次开发**没有新增业务功能**，产品形态保持不变（单用户、同步请求链路、SQLite 单表、Docker Compose 三容器），重点是把「LLM 接入后端系统」这条链路做得可验证、可观测、可维护：

| 方向 | 改动前 | 改动后 |
|---|---|---|
| 模型行为 | 依赖模型服务未承诺的默认行为，前端偶发拿不到结果 | 思考模式显式固化并做成配置开关，5 条契约测试守住 |
| 可测性 | 全量 `pytest` 跑不完、依赖真密钥与外网 | `conftest.py` 四层隔离，断网无密钥 131 passed |
| 回归门禁 | 只有本机跑测试 | GitHub Actions + 覆盖率门禁 65 |
| 评估能力 | 只评检索，不评生成，成本无分项 | 18 条样例 × 3 轮消融 + 5 指标生成质量评估 + 成本看板 |
| 缓存正确性 | key 不含模型 / 知识库版本，换模型会命中旧结果 | 版本指纹自动捕获，15 条契约测试 |
| 性能 | 地图 enrich 约 18 次高德请求串行 | 线程池并行，同环境 A/B −28.1% 且零降级 |
| 文档可信度 | README 有 4 处与源码不符 | 逐条对齐源码，历史指标标注样本数 |

## 二、运行环境

后端通过 `backend/.env` 配置模型与密钥（该文件不入库，模板见 `backend/.env.example`）。当前使用阿里云百炼 OpenAI 兼容接口：

| 角色 | 模型 |
|---|---|
| Chat（Query Rewrite / Planner） | `qwen3.7-plus` |
| Embedding | `qwen3.7-text-embedding` |
| Rerank | `qwen3-rerank` |

上游 README / CHANGELOG 中的 `qwen-max`、`text-embedding-v4` 属于历史迭代记录，不代表当前配置。后端全部为同步 `def`，无 async / 无流式；前端 Vue 3，未引入 vue-router / pinia。

## 三、改动明细

### 3.1 固化 LLM 调用行为，并让它可验证

`1f218bc` · `c1e09eb` · `c764cb6`

`POST /trip/generate` 一度出现「后端链路走完、前端拿不到结果」。分段定位确认 Query Rewrite、Embedding、Chroma 召回、Rerank 均成功，卡点在最后一次 Planner 调用：`qwen3.7-plus` 是混合思考模型，默认开启 thinking，思维链 token 计入输出并显著拉长耗时。同一把 key、同一句 14-token 短问题的 A/B：

| 调用方式 | 耗时 | `reasoning_content` | token（输入/输出/总） |
|---|---|---|---|
| 默认 | 8.67s | 697 字符，`reasoning_tokens=380` | 14 / 405 / 419 |
| `enable_thinking: false` | 1.62s | 无 | 16 / 23 / 39 |

放大因素是 `LLM_TIMEOUT_SECONDS=60` × 最多 2 次尝试 = 120s，恰好等于前端 `axios timeout: 120000`；知识库由 49 chunk 扩到 79、Planner prompt 变长后，延迟越过了这条线。

改动：两处 `_build_chat_llm()` 显式传入 `enable_thinking: False`，前端超时 120s → 180s。随后把写死的参数收敛成 `LLM_ENABLE_THINKING` 配置项（默认 `false`），并用 5 条契约测试守住「配置原样下传到两个调用点」与「不设 / `true` / `false` 三种 env 解析」。默认行为与初版修复完全一致。

**取舍**：把外部服务的默认行为固化在自己的代码里，而不是依赖上游不承诺的默认值；开关不改用户可见行为，改的是「能不能复现」和「谁说了算」。

### 3.2 检索与生成的评估体系

**消融实验与成本分项** · `87088bb`

`evaluate_rag_retrieval.py` 增加 `--no-rewrite` / `--no-rerank`（只在评估脚本层旁路，不动生产代码），补上原本漏记的 rewrite token 分项。18 条自建评估样例 × 3 轮取中位数、`REDIS_ENABLED=false` 冷链路：

| 模式 | top1 | topK | MRR | 检索耗时(ms) | rerank token | rewrite token |
|---|---|---|---|---|---|---|
| baseline | 88.9% | 100.0% | 0.928 | 1494.1 | 31803 | 2206 |
| `--no-rewrite` | 88.9% | 100.0% | 0.944 | 1687.0 | 33654 | 0 |
| `--no-rerank` | 83.3% | 94.4% | 0.889 | 853.4 | 0 | 2206 |

重排占 baseline 总 prompt token 约 92%；关掉后检索耗时降 42.9%，代价是 top1 掉 5.6 个百分点、MRR 0.928 → 0.889 —— 量化了「用成本换质量」，但**没有据此删除该环节**。`avg_latency_ms` 只量「召回 + 重排」，不含 rewrite 的 LLM 调用，不是端到端 `POST /trip/generate` 耗时。

**缓存 key 的版本维度** · `961cb9f`

原 RAG key 与 rerank key 都不含模型名 / 知识库版本，换 embedding 模型会命中旧结果。新增 `_cache_version_stamp()`，把 `EMBEDDING_MODEL` / `RERANK_MODEL` / `CHROMA_COLLECTION_NAME` / `KNOWLEDGE_BASE_VERSION` 算成指纹插进两类 key，例如 `rag:guide:v1:bbdfd4a395fc:{dest}:{query}:{top_k}`。换模型由指纹自动捕获，手动 bump 只针对「重建索引但模型名没变」。15 条契约测试覆盖 bump 后旧 key 不命中、同版本内仍可复用。

**生成质量评估** · `5bd8c71`

新增纯标准库、零网络 / 零模型 / 零 DB 的离线评估脚本：5 个规则指标（天数完整性 / 预算不超 / 景点数达标 / 天气提示一致性 / 偏好关键词命中）逐条 0~1 打分 → 总分 0~100，汇总含中位数。默认 6 条样例是**手工构造**的，用于验证评估脚本与规则基线，不是模型成绩单。「天气提示一致性」检查的是架构约定：天气来自独立 `GET /weather/forecast`，后端 `tips` 不应写死天气词。

**成本数据聚合与看板** · `71adb6a` · `b26e7f2`

新增 `GET /metrics/dashboard`：把 A/B 汇总、端到端计时与 `/trip/stats` 合成一份结构化响应，三段各有 `available` + `note` + `sources`，顶层 `status=ok/partial/empty`，异常一律降级不返回 500。A/B 原始日志保存在本地开发文档目录（不入库），因此把汇总作为**静态快照**放进 `backend/data/metrics/`，同时实现「文件缺失 → 明确空状态」，不拿 0 冒充数据。前端加第 4 个「成本」Tab 消费同一份契约链（Pydantic schemas → `api.ts` → `types/index.ts` → Vue），不引图表库、不派生「快了 N%」这类指标。

### 3.3 测试与 CI

`2cac51d` · `b833098` · `c764cb6` · `123b008` · `57a50ca`

- `backend/tests/conftest.py` 四层隔离：强制置空密钥 / 关 Redis 与地图增强、注入假 LLM 模块、Chroma collection 返回 `None`、封死 `httpx` 出口；并把 `scripts/` 下的手动调试脚本排除出 pytest 收集（裸跑的 `2 deselected` 由此而来，不是跳过真测试）。
- GitHub Actions：push `main` / PR 触发，Python 3.13 + `pytest-cov`（CI 单独安装，不改 `requirements.txt`），`--cov-fail-under=65`。
- 4b 补 7 条端点契约测试，覆盖 health / root / stats / delete 成功与失败分支 / weather 降级分支；每个测试自己造数据，不依赖共享 `app.db` 与执行顺序。

当前：**131 passed / 2 deselected / 73.54%**（2434 stmts / 644 miss），门禁 65 余量 8.54pp；`.env` 改名 + 封 `socket.create_connection` 下仍 131 passed、外部 TCP 连接尝试 0 次。

**口径**：CI 绿勾证明的是「断网、无密钥、全新依赖下测试集可重复跑完」，不等于真密钥联网时的端到端可用。

### 3.4 地图 enrich 并行化

`3928680` · `39ec674`

端到端耗时里，地图 enrich 约 18 次高德 HTTP **串行**占 8–14s，检索只占 1–2% —— 瓶颈是排队等网络回包，不是 RAG。把嵌套 for 改成「收集任务 → `ThreadPoolExecutor` + `as_completed` → 主线程汇总」，`MAP_ENRICH_MAX_WORKERS` 可配（1–16；`workers<=1` 走原串行分支，连线程池都不建，因此 A/B 只需改一个 env 值重启，不用改代码）。

同环境单变量各 3 次、取中位数：

| 并发 | 端到端中位数 | 降级 |
|---|---|---|
| 1（串行） | 69.74s | 0 项 |
| **3（默认）** | **50.17s（−28.1%）** | **0 项** |
| 6 | 42.51s（−39.0%） | 8 项 `AmapServiceError`，字段缺失 |

6 路更快但触发高德限流 / 代理并发限制导致降级，所以默认值定为 **3**。取舍：不改 async（后端全同步，这批是 I/O 等待，线程池足够）；单项 `try/except` 隔离，一处失败不影响整份行程；日志只记异常类型不记文本（高德请求 URL 带 API Key）。

**口径**：这批 A/B 全流量走代理，串行基线因此高于此前 28.82s 的批次，跨环境数字不可直接相减。

### 3.5 前端改动与文档可信度

`1f218bc` · `b26e7f2` · `f44c7d6` · `6af5853` · `57a50ca`

前端侧共 3 组实质改动，另有 1 处超时配合：

| commit | 改动 | 主要文件 |
|---|---|---|
| `1f218bc` | axios 超时 120s → 180s，配合后端思考模式修复 | `services/api.ts` |
| `b26e7f2` | 新增第 4 个「成本」Tab：加载 / 失败 / 空三态，展示 A/B 对比、端到端计时与 token 构成 | `views/Metrics.vue`（新增）、`App.vue`、`services/api.ts`、`types/index.ts` |
| `f44c7d6` | 视觉规范收口：删 emoji / 毛玻璃 / 阴影 / 按压缩放，字号从 9 档收敛到 12 / 14 / 17 / 22 四档，状态与错误色退中性灰 | 6 个视图与组件、`DESIGN.md` |

跨端改数据结构走的是完整契约链：Pydantic `schemas.py` → 服务层 → `services/api.ts` → `types/index.ts` → Vue 渲染，五处同步；成本页新增的 9 个类型与后端 `metrics_schemas.py` 字段一一对应。前端未引入 vue-router / pinia / 图表库，视图切换由 `App.vue` 管理，表格与条形图用纯 CSS 实现；`App.vue` 里原先用 `v-else` 兜底的 History 改成显式 `v-else-if`，否则切到成本页会连历史页一起渲染。成本页精简时**刻意保留**样本数（18 条 × 3 轮中位数）、「数据缺失」空状态与检索耗时口径；顺带修掉 390px 视口下 `min-width:720px` 表格把布局撑开的响应式 bug。

文档可信度（`6af5853` / `57a50ca`）：

- README 4 处与实现不符的修正：攻略切片 49 → 79 chunk；天气从主链路移除（独立 `GET /weather/forecast`，雨天提示由前端动态增删）；Markdown 导出是 f-string 拼接而非 Jinja2；CHANGELOG 历史指标标注「15 条样例时期，当前 18 条」。
- CI 注释中的测试数 / 覆盖率口径同步到 131 / 73.54%。

## 四、复现

```bash
# 后端依赖
cd backend && pip install -r requirements.txt

# 全量测试（断网无密钥可跑）
python -m pytest -q

# 与 CI 相同的覆盖率门禁
python -m pytest -q --cov=app --cov-report=term-missing --cov-fail-under=65

# RAG 评估（18 条样例，需配置模型密钥）
python scripts/evaluate_rag_retrieval.py
python scripts/evaluate_rag_retrieval.py --no-rerank

# 生成质量评估（离线，自带自检）
python scripts/evaluate_generation_quality.py --self-test

# 前端构建
cd frontend && npm ci && npm run build
```

## 五、指标口径

- RAG 评估集 **18 条**（6 城 × 3），消融实验 18 条 × 3 轮取中位数、同环境同批次单变量。CHANGELOG 里的 80% → 93.3% 等指标来自 **15 条样例时期**且模型配置不同，两组数字不可直接比较。
- `avg_latency_ms` 只含「召回 + 重排」两段；端到端耗时另看计时数据，两者不是一回事。
- 成本看板展示的是仓库内静态快照（2026-11-08 批次），不是实时测量。
- 所有 A/B 数字均标注环境；跨环境（解释器、网络路径、代理）的数字不可相减。

## 六、数据边界与尚未覆盖

6 个本地 Markdown 攻略仅作 RAG 参考知识，动态城市的地点实体来自当次高德 POI 候选。门票、餐饮、住宿、交通与酒店价格均为**规划估算**，不代表营业状态、可预订性或实时核验价格；项目无真实线上流量，所有指标来自本地自测与自建评估集。

尚未做：SSE 流式输出、鉴权与多用户、模型 pin 到具体快照、依赖版本完全锁定、真实线上数据验证。测试覆盖较低的部分：`metrics_service`、`weather_service`、`export_service`、`map_service`。

## 七、提交索引

| 主题 | commit |
|---|---|
| 关闭思考模式 / 配置开关 / env 契约 | `1f218bc`、`c1e09eb`、`c764cb6` |
| RAG 消融与 token 分项 | `87088bb` |
| 统一 Mock、CI 门禁、端点契约、口径同步 | `2cac51d`、`b833098`、`123b008`、`57a50ca` |
| 生成质量评估、看板聚合、缓存 key 版本、前端成本页 | `5bd8c71`、`71adb6a`、`961cb9f`、`b26e7f2` |
| README 可信度修正 | `6af5853` |
| 地图 enrich 并行化与默认值调整 | `3928680`、`39ec674` |
| 前端视觉规范收口 | `f44c7d6` |

完整改动对比：[compare/73378d5...main](https://github.com/P1asticLester/zhilv-yuntu/compare/73378d5...main)
