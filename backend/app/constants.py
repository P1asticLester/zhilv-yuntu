"""跨模块共用的常量：同一条规则只在这里定义一次。"""

from __future__ import annotations

# 面向用户的 tips / source_notes 里不应出现的内部实现说明。
# 后端在生成与导出时统一过滤，前端不再自带一份关键词表。
TECHNICAL_TIP_KEYWORDS = (
    "LLM",
    "RAG",
    "LangChain",
    "Chroma",
    "演示",
    "测试",
    "规则",
    "模型",
    "源码",
    "trip_service",
)

TRIP_NOT_FOUND_DETAIL = "Trip not found."

# schema 允许这些字段为 None，服务层与 prompt 统一用同一套兜底值。
DEFAULT_PACE = "适中"
DEFAULT_HOTEL_LEVEL = "舒适型"
DEFAULT_PREFERENCE_TEXT = "常规旅行体验"

# 规则行程的默认条目，动态行程与 RAG 行程共用。
DEFAULT_SPOT_START_TIME = "10:00"
DEFAULT_SPOT_END_TIME = "12:00"
DEFAULT_MEAL_TYPE = "午餐"
DEFAULT_TRANSPORT_DURATION = "30 分钟"

# 切片与重排时视为低信息量噪声的片段标题。
NOISE_CHUNK_TITLE = "文档开头"
NOISE_CHUNK_TITLES = frozenset({NOISE_CHUNK_TITLE})

# 检索 query 的关键词切分规则。
QUERY_SPLIT_PATTERN = r"[\s,，。；;、]+"
