# AGENTS.md

面向中文旅行场景的 AI 旅行规划系统：FastAPI（全同步 `def`）+ Vue 3 + SQLite / ChromaDB，
用 LLM 与 RAG 生成行程，支持高德 POI 回填、天气查询与 Markdown / PDF 导出。
本仓库是 fork 后的二次开发版本，改动索引见 [FORK_CHANGES.md](FORK_CHANGES.md)。

## 目录路标

- `backend/app/api/routes/` HTTP 端点；`backend/app/services/` 行程、地图、天气、导出、存储。
- `backend/app/agents/` Planner 与 Query Rewrite；`backend/app/rag/` 切片、向量检索、重排。
- `backend/app/models/schemas.py` Pydantic v2 契约；`backend/tests/` 断网无密钥可跑的全量测试。
- `frontend/src/views/` 页面（无 vue-router / pinia）；`frontend/src/services/api.ts` 唯一 API 入口。
- `docs/` 本地扩展阅读，**不纳入版本控制**，clone 后不存在。
- `backend/db/app.db` 不进 Git，首次读写时惰性建表；Docker 用命名卷持久化。

## 改代码前先看

- **契约链五处同步**：`schemas.py` → 服务层 → `frontend/src/services/api.ts` → `types/index.ts` → Vue 渲染。改数据结构必须五处一起改。
- **共享模块是单一出处**：`app/constants.py`（常量与兜底值）、`app/text_utils.py`（纯文本工具）、`app/agents/llm_client.py`（LLM 构造与 token usage）、`app/services/amap_client.py`（高德 HTTP 共用件）。
  **写新函数、新规则前，先搜项目里有没有现成的**，不要在服务层再复制一份。
- 模块级配置常量（`LLM_MODEL`、`AMAP_API_KEY` 等）由各模块读好后传进共享构造函数；测试会 monkeypatch 这些模块级名字，清理 import 时别顺手删掉。
- 天气不在 `/trip/generate` 主链路，是独立的 `GET /weather/forecast`；雨天提示由前端增删 tips。
- 后端全部是同步 `def`，没有 async，也没有 SSE 流式。

## 验证

```sh
cd backend
.venv/Scripts/python.exe -m pytest tests/ -q
```

Mock 统一在 `backend/tests/conftest.py`，断网、无 API Key 也能跑绿。只测用户感受得到的行为。

## 提交前钩子

启用一次（本机配置，不随仓库走）：

```sh
git config core.hooksPath .githooks
```

钩子会拒绝「暂存的文本超过 20 KB（约 5,000 token）」的提交，报错里写明文件多大；lockfile、依赖、构建产物、二进制资源跳过不查。
**提交被钩子拒了，就照报错按功能拆成几个文件再提交**，不要调大阈值、不要把文件加进豁免清单。
装钩子前就超标的存量文件列在 `.githooks/size_allowlist.txt`，已豁免；不要新增条目。
