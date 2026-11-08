"""⑦ 7a 契约测试：RAG / Rerank 缓存 key 必须带模型名与知识库版本维度。

不连 Redis、不调模型、不联网：把 get/set_cached_json 换成内存假缓存，
只验证「交给缓存层的 key 长什么样、旧结果会不会被读到」。

对应 bug（docs/mastery/06 的 D8）：换 embedding / rerank 模型或重建索引后，
query 与 chunks 可能完全不变，旧 key 仍会命中，返回过期结果。

覆盖三条验收：
1. key 含版本段；
2. 四个版本维度任一变化，两类 key 都不同；
3. 旧版本 / 升级前无版本的 key 不再命中，同版本内仍可复用。
"""

from pathlib import Path
import hashlib
import re
import sys

import pytest


# 允许测试文件直接导入 backend/app 下的模块。
CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.rag.retriever as retriever  # noqa: E402


EMPTY_USAGE = {"prompt_tokens": 0, "completion_tokens": 0}

CHUNKS = [
    {"source": "dali_guide.md", "title": "苍山", "text": "苍山可徒步。"},
    {"source": "dali_guide.md", "title": "洱海", "text": "洱海可骑行。"},
]

# 四个维度都参与版本段；任一变化都必须让 key 变化。
VERSION_DIMENSIONS = (
    "EMBEDDING_MODEL",
    "RERANK_MODEL",
    "CHROMA_COLLECTION_NAME",
    "KNOWLEDGE_BASE_VERSION",
)


class _FakeRedisCache:
    """最小内存缓存替身：记录读过的 key，模拟 Redis 里残留的历史结果。"""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.read_keys: list[str] = []

    def get(self, key: str):
        self.read_keys.append(key)
        return self.store.get(key)

    def set(self, key: str, value, expire_seconds=None) -> None:
        self.store[key] = value


@pytest.fixture
def fake_cache(monkeypatch):
    cache = _FakeRedisCache()
    monkeypatch.setattr(retriever, "get_cached_json", cache.get)
    monkeypatch.setattr(retriever, "set_cached_json", cache.set)
    return cache


def _readable_version() -> str:
    """复现版本段里可读的那一段（配置值被清洗过，不能直接断言原值）。"""
    return re.sub(r"[^0-9A-Za-z._-]", "-", retriever.KNOWLEDGE_BASE_VERSION) or "unversioned"


def _legacy_rag_key(query: str, top_k: int, destination: str | None) -> str:
    """升级前的旧格式 key（无版本段），用来模拟 Redis 残留。"""
    return f"rag:guide:{destination or 'all'}:{retriever._normalize_cache_text(query)}:{top_k}"


def _legacy_rerank_key(query: str, chunks: list[dict[str, str]]) -> str:
    """升级前的旧格式 rerank key（无版本段）。"""
    fingerprint = "|".join(
        f"{c.get('source', '')}:{c.get('title', '')}" for c in chunks
    )
    digest = hashlib.md5(fingerprint.encode()).hexdigest()[:12]
    return f"rerank:{retriever._normalize_cache_text(query)}:{digest}"


# ------------------------------------------------------------------ 1. 含版本


def test_rag_cache_key_contains_version_stamp():
    """RAG key 带版本段，且保留 destination / 归一化 query / top_k 三个原有维度。"""
    key = retriever._build_rag_cache_key(" 大理  古城 ", 3, "大理")
    stamp = retriever._cache_version_stamp()

    assert key.startswith(f"rag:guide:{stamp}:")
    assert key.endswith(":大理:大理 古城:3")
    assert stamp.split(":")[0] == _readable_version()
    assert len(stamp.split(":")) == 2  # 可读版本号 + 12 位摘要


def test_rerank_cache_key_contains_version_stamp():
    """Rerank key 带版本段，且保留归一化 query 与 chunks 内容哈希。"""
    key = retriever._build_rerank_cache_key(" 大理 自然风景 ", CHUNKS)
    stamp = retriever._cache_version_stamp()
    fingerprint = "|".join(f"{c['source']}:{c['title']}" for c in CHUNKS)
    chunks_hash = hashlib.md5(fingerprint.encode()).hexdigest()[:12]

    assert key.startswith(f"rerank:{stamp}:")
    assert key.endswith(f":大理 自然风景:{chunks_hash}")
    assert stamp.split(":")[0] == _readable_version()


# ------------------------------------------------------------ 2. 换版本 key 变


@pytest.mark.parametrize("dimension", VERSION_DIMENSIONS)
def test_version_stamp_changes_with_every_dimension(monkeypatch, dimension):
    """四个维度任一变化，版本段都必须变化（不靠 query / chunks 变化兜底）。"""
    before = retriever._cache_version_stamp()
    monkeypatch.setattr(retriever, dimension, "changed-for-test")
    after = retriever._cache_version_stamp()

    assert after != before


@pytest.mark.parametrize("dimension", VERSION_DIMENSIONS)
def test_both_cache_keys_change_when_version_changes(monkeypatch, dimension):
    """换模型 / 换集合 / bump 知识库版本后，两类 key 都不能沿用旧值。"""
    rag_before = retriever._build_rag_cache_key("大理 古城", 3, "大理")
    rerank_before = retriever._build_rerank_cache_key("大理 古城", CHUNKS)

    monkeypatch.setattr(retriever, dimension, "changed-for-test")

    assert retriever._build_rag_cache_key("大理 古城", 3, "大理") != rag_before
    assert retriever._build_rerank_cache_key("大理 古城", CHUNKS) != rerank_before


def test_keys_are_stable_within_same_version():
    """同一版本内 key 稳定（否则缓存永远无法复用，7b 就测不出收益）。"""
    assert (
        retriever._build_rag_cache_key("大理 古城", 3, "大理")
        == retriever._build_rag_cache_key(" 大理   古城 ", 3, "大理")
    )
    assert (
        retriever._build_rerank_cache_key("大理 古城", CHUNKS)
        == retriever._build_rerank_cache_key(" 大理  古城 ", CHUNKS)
    )

    # 原有维度仍然生效，没有被版本段挤掉。
    assert (
        retriever._build_rag_cache_key("大理 古城", 3, "大理")
        != retriever._build_rag_cache_key("大理 美食", 3, "大理")
    )
    assert (
        retriever._build_rag_cache_key("大理 古城", 3, "大理")
        != retriever._build_rag_cache_key("大理 古城", 5, "大理")
    )
    assert (
        retriever._build_rag_cache_key("大理 古城", 3, "大理")
        != retriever._build_rag_cache_key("大理 古城", 3, None)
    )
    assert (
        retriever._build_rerank_cache_key("大理 古城", CHUNKS)
        != retriever._build_rerank_cache_key("大理 古城", CHUNKS[:1])
    )


# -------------------------------------------------- 3. 旧 key 不命中 / 同版本复用


def test_rag_cache_reuses_within_version_but_misses_after_bump(
    monkeypatch, fake_cache
):
    """端到端：同版本第二次命中；bump 知识库版本后不读旧结果、重新检索。"""
    calls: list[str] = []

    def fake_retrieve(query, top_k=3, destination=None):
        calls.append(query)
        return [{"source": "new.md", "title": "新片段", "text": "新内容"}], EMPTY_USAGE, EMPTY_USAGE

    monkeypatch.setattr(retriever, "retrieve_travel_guide_chunks", fake_retrieve)

    first, _, _ = retriever.retrieve_travel_guide("大理 古城", top_k=3, destination="大理")
    assert len(calls) == 1
    assert len(fake_cache.store) == 1
    version_v1_key = next(iter(fake_cache.store))

    second, _, _ = retriever.retrieve_travel_guide("大理 古城", top_k=3, destination="大理")
    assert second == first  # 同版本命中，没有重复算
    assert len(calls) == 1
    assert fake_cache.read_keys[-1] == version_v1_key

    # 重建索引后 bump 版本：旧缓存仍在 Redis 里，但不该被读到。
    monkeypatch.setattr(retriever, "KNOWLEDGE_BASE_VERSION", "v2")
    third, _, _ = retriever.retrieve_travel_guide("大理 古城", top_k=3, destination="大理")
    assert third == first  # 检索结果内容相同（假检索固定）
    assert len(calls) == 2  # 关键是重新算了，没走缓存
    assert fake_cache.read_keys[-1] != version_v1_key
    assert _readable_version() in fake_cache.read_keys[-1]
    assert len(fake_cache.store) == 2  # 新旧两批 key 并存，旧的自然过期


def test_legacy_unversioned_rag_key_is_never_hit(monkeypatch, fake_cache):
    """升级前写入的无版本 key，升级后一次都不该被读取。"""
    legacy = _legacy_rag_key("大理 古城", 3, "大理")
    fake_cache.store[legacy] = ["升级前的过期结果"]

    calls: list[str] = []

    def fake_retrieve(query, top_k=3, destination=None):
        calls.append(query)
        return [{"source": "new.md", "title": "新片段", "text": "新内容"}], EMPTY_USAGE, EMPTY_USAGE

    monkeypatch.setattr(retriever, "retrieve_travel_guide_chunks", fake_retrieve)

    results, _, _ = retriever.retrieve_travel_guide("大理 古城", top_k=3, destination="大理")

    assert results == ["[来源: new.md | 标题: 新片段]\n新内容"]
    assert len(calls) == 1
    assert legacy not in fake_cache.read_keys
    assert all(retriever._cache_version_stamp() in key for key in fake_cache.read_keys)


def test_rerank_cache_reuses_within_version_but_misses_after_model_change(
    monkeypatch, fake_cache
):
    """Rerank 侧同样：同版本命中，换 rerank 模型后旧排序结果不再被读到。"""
    calls: list[str] = []

    def fake_rerank(query, chunks, top_k):
        calls.append(query)
        return [(0.9, 0), (0.5, 1)], EMPTY_USAGE

    monkeypatch.setattr(retriever, "_rerank_with_dashscope", fake_rerank)

    first, _ = retriever.rerank_guide_chunks("大理 古城", CHUNKS, top_k=2)
    assert len(calls) == 1
    assert len(fake_cache.store) == 1
    version_v1_key = next(iter(fake_cache.store))

    second, _ = retriever.rerank_guide_chunks("大理 古城", CHUNKS, top_k=2)
    assert [c["rerank_score"] for c in second] == [c["rerank_score"] for c in first]
    assert len(calls) == 1
    assert fake_cache.read_keys[-1] == version_v1_key

    monkeypatch.setattr(retriever, "RERANK_MODEL", "another-rerank-model")
    third, _ = retriever.rerank_guide_chunks("大理 古城", CHUNKS, top_k=2)
    assert len(calls) == 2  # 重新算了，没拿旧模型的排序
    assert fake_cache.read_keys[-1] != version_v1_key
    assert [c["rerank_score"] for c in third] == [c["rerank_score"] for c in first]


def test_legacy_unversioned_rerank_key_is_never_hit(monkeypatch, fake_cache):
    """升级前写入的无版本 rerank key，升级后不该命中。"""
    legacy = _legacy_rerank_key("大理 古城", CHUNKS)
    fake_cache.store[legacy] = [{"i": 0, "s": 9.9}]

    calls: list[str] = []

    def fake_rerank(query, chunks, top_k):
        calls.append(query)
        return [(0.9, 0), (0.5, 1)], EMPTY_USAGE

    monkeypatch.setattr(retriever, "_rerank_with_dashscope", fake_rerank)

    results, _ = retriever.rerank_guide_chunks("大理 古城", CHUNKS, top_k=2)

    assert legacy not in fake_cache.read_keys
    assert len(calls) == 1
    assert all(
        "cross-encoder:9.9000" not in c.get("rerank_reasons", [])
        for c in results
    )
    assert all(retriever._cache_version_stamp() in key for key in fake_cache.read_keys)
