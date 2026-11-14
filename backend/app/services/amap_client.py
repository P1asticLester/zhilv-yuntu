"""高德 HTTP 访问的共用件：Key 检查、客户端、缓存文本标准化。

地图与天气两条链路对「调用失败」抛不同异常、给不同提示，因此请求与错误
处理仍各留在自己的服务里；这里只收拢与业务无关的共同部分。
"""

from __future__ import annotations

import httpx


def has_amap_api_key(api_key: str) -> bool:
    """由调用方传入自己读到的配置值，便于测试替换模块级常量。"""
    return bool(api_key)


def build_client(timeout: int) -> httpx.Client:
    """创建访问高德 API 的客户端；超时由调用方从自己的模块级配置传入。"""
    return httpx.Client(timeout=timeout)


def normalize_cache_text(value: str | None) -> str:
    """把缓存 key 里用到的文本做简单标准化。"""
    if value is None:
        return ""
    return value.strip().lower()
