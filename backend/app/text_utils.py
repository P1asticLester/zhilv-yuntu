"""跨模块共用的小文本工具；只放与业务无关的纯函数。"""

from __future__ import annotations

import re

from app.constants import QUERY_SPLIT_PATTERN


def split_query_keywords(query: str) -> list[str]:
    """把 query 切成用于关键词匹配 / 轻量重排的词列表。"""
    return [
        part.strip()
        for part in re.split(QUERY_SPLIT_PATTERN, query)
        if part.strip()
    ]


def append_unique(items: list[str], value: str | None) -> None:
    """去空白、跳过空值、去重后追加到列表。"""
    normalized = (value or "").strip()
    if normalized and normalized not in items:
        items.append(normalized)


def format_minutes(minutes: object) -> str:
    """把分钟数渲染成面向用户的耗时文本。"""
    return f"{minutes} 分钟"
