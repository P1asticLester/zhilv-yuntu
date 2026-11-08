"""④ 4a · 统一测试夹具：让 pytest 在「断网 + 无密钥」环境下全绿。

这里只替换「外部世界」，不改产品代码，也不放宽任何一条测试断言。

| 外部依赖 | 在这里怎么处理 | 测试实际跑到的是什么 |
| --- | --- | --- |
| LLM（ChatOpenAI / embedding） | 置空 LLM_API_KEY + 注入假的 langchain_openai 模块 | 产品代码自己的「模型不可用」降级路径（规则改写 / RAG 兜底行程） |
| 高德地图（含天气） | 置空 AMAP_API_KEY、关 ENABLE_AMAP_ENRICHMENT、封死 httpx 出口 | 不发 HTTP；个别测试自己 monkeypatch 假响应来测错误处理 |
| Chroma 向量库 | _get_chroma_collection 固定返回 None | 纯本地关键词检索，不依赖本机是否灌过向量 |
| Redis | REDIS_ENABLED=false | 每次都是冷链路，结果可复现 |

环境变量必须在任何 app.* 模块 import 之前设置：app/config.py 顶层会执行
load_dotenv(backend/.env)，而 python-dotenv 默认 override=False，
所以这里先占位的键不会被 .env 里的真实密钥覆盖。

单个测试若想测「有模型 / 有地图」的分支，照常用 monkeypatch 覆盖即可
（test_llm_thinking_switch.py、test_trip_planner_agent.py 已经这么做）：
autouse fixture 的替换发生在它之前，后设的会覆盖它，测完一起还原。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest


# ---------------------------------------------------------------- 1. 环境隔离
# 用赋值而不是 setdefault：即使 CI / 本机提前注入了真实密钥，测试进程里也要
# 强制清空，否则「无密钥」这个前提不成立，测试就会偷偷联网。
os.environ["LLM_API_KEY"] = ""
os.environ["AMAP_API_KEY"] = ""
os.environ["REDIS_ENABLED"] = "false"
os.environ["ENABLE_AMAP_ENRICHMENT"] = "false"


TESTS_DIR = Path(__file__).resolve().parent


# ------------------------------------------------------------ 2. 假的 LLM 模块
class _FakeChatOpenAI:
    """记录构造参数；invoke 返回空内容，逼产品代码走规则降级。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def invoke(self, messages):
        return SimpleNamespace(
            content="",
            response_metadata={
                "token_usage": {"prompt_tokens": 0, "completion_tokens": 0}
            },
        )


class _FakeOpenAIEmbeddings:
    """embedding 替身；无密钥时产品代码直接跳过，正常测试不会用到。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def embed_query(self, text):
        return [0.0]

    def embed_documents(self, texts):
        return [[0.0] for _ in texts]


def _fake_langchain_openai() -> ModuleType:
    module = ModuleType("langchain_openai")
    module.ChatOpenAI = _FakeChatOpenAI
    module.OpenAIEmbeddings = _FakeOpenAIEmbeddings
    return module


# ------------------------------------------------------- 3. 每个测试自动生效
@pytest.fixture(autouse=True)
def offline_external_services(monkeypatch):
    """统一 Mock LLM / 高德 / Chroma，并封死真实 HTTP 出口。"""
    # LLM：默认给所有测试一个假模块；测试自己注入 fake 模块时会覆盖它。
    monkeypatch.setitem(sys.modules, "langchain_openai", _fake_langchain_openai())

    # Chroma：固定返回 None → search_guide_chunks_with_usage 退回本地关键词检索，
    # 不依赖 db/chroma_db 是否存在，也不依赖 chromadb 是否装好。
    from app.rag import vector_db

    monkeypatch.setattr(vector_db, "_get_chroma_collection", lambda: None)

    # 高德 / 天气 / 任何在线 HTTP：保险丝。漏网的请求立刻失败并给出明确原因，
    # 而不是静默等 30s 超时。TestClient 重写了 request()，不受影响。
    def _blocked_request(*_args, **_kwargs):
        raise RuntimeError(
            "测试期间禁止真实 HTTP 请求：请在测试里用 monkeypatch 提供替身。"
        )

    monkeypatch.setattr(httpx.Client, "request", _blocked_request)
    monkeypatch.setattr(httpx.AsyncClient, "request", _blocked_request)


# --------------------------------------------- 4. 排除 scripts/ 下的调试脚本
def pytest_collection_modifyitems(config, items):
    """backend/scripts/test_*.py 是手动调试脚本，混进 pytest 会误触发真实请求。"""
    kept = []
    dropped = []
    for item in items:
        path = Path(str(item.path)).resolve()
        # 判断「在 tests/ 之下（含将来新增的子目录）」，别写死 parent，
        # 否则 4b 若在 tests/api/ 下加端点测试会被误排除。
        if path == TESTS_DIR or TESTS_DIR in path.parents:
            kept.append(item)
        else:
            dropped.append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
