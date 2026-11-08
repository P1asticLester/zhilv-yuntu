"""6a · 看板后端只读聚合接口。

只提供一个 ``GET /metrics/dashboard``，把 A/B 汇总、端到端计时、/trip/stats
合成一份结构化 JSON。只读、无请求参数、无副作用、无网络调用。

不碰前端（6b 待拍板）；不改 retriever.py / schemas.py 等主链路文件。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.models.metrics_schemas import MetricsDashboardResponse
from app.services.metrics_service import build_dashboard

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/dashboard", response_model=MetricsDashboardResponse)
def get_metrics_dashboard() -> MetricsDashboardResponse:
    """返回看板聚合数据；数据文件缺失时返回明确空状态而非报错。"""
    return build_dashboard()
