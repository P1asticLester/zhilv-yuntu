"""6a · 看板后端只读聚合接口的响应契约。

独立成文件，不改动 ``app/models/schemas.py``（主链路契约文件零改动）。
本模块只描述「读出来的数据长什么样」，不含任何业务逻辑，也不触发外部调用。

设计取舍（拍板 2026-11-08 18:27）
--------------------------------
* 只做 6a 后端聚合，**不碰 6b 前端**：前端 api.ts / types/index.ts / Vue 一律不动，
  因此这里不需要与前端契约链同步；等 6b 拍板时再按五处同步走。
* 三段数据（A/B 汇总、端到端计时、已保存行程 token 统计）都可能缺失：
  每段自带 ``available`` + ``note``，顶层用 ``status`` 表达整体可用度，
  文件缺失返回明确空状态，绝不假装稳定或抛 500。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AblationRow(BaseModel):
    """ablation_summary.csv 的一行（字段名与 CSV 列一一对应，不做单位换算）。"""

    mode: str = Field(..., description="消融模式：base / norew / norr")
    rounds_ok: int = Field(default=0, ge=0, description="成功轮数")
    redis_enabled: bool = Field(default=False, description="该批次是否启用 Redis 缓存")
    cases: int = Field(default=0, ge=0, description="评估样例数")
    top1_pct: float = Field(default=0.0, description="top1 命中率（百分数）")
    topk_pct: float = Field(default=0.0, description="topK 命中率（百分数）")
    mrr: float = Field(default=0.0, description="MRR")
    avg_latency_ms: float = Field(default=0.0, description="检索阶段平均耗时（毫秒）")
    embedding_tokens: float = Field(default=0.0, description="embedding prompt token")
    rerank_tokens: float = Field(default=0.0, description="rerank prompt token")
    rewrite_prompt_tokens: float = Field(default=0.0, description="LLM 改写 prompt token")
    rewrite_completion_tokens: float = Field(default=0.0, description="LLM 改写 completion token")


class TimingRow(BaseModel):
    """timing_result.csv 的一行（端到端 /trip/generate 计时）。"""

    run: int = Field(default=0, description="第几次")
    status: int = Field(default=0, description="HTTP 状态码")
    seconds: float = Field(default=0.0, description="端到端耗时（秒）")
    trip_id: str = Field(default="", description="行程 ID")
    days: int = Field(default=0, description="生成天数")
    prompt_tokens: int = Field(default=0, description="prompt token")
    completion_tokens: int = Field(default=0, description="completion token")
    planner_out: int = Field(default=0, description="Planner 输出长度")
    error: str = Field(default="", description="错误信息，成功时为空")


class DataSourceInfo(BaseModel):
    """单个数据源的可用状态与来源说明。"""

    name: str = Field(..., description="数据源标识")
    location: str = Field(..., description="读取位置（相对仓库根，尽量不暴露绝对路径）")
    available: bool = Field(..., description="是否成功读到数据")
    kind: str = Field(..., description="snapshot=仓库内静态快照 / live=实时服务层 / missing=缺失")
    note: str = Field(default="", description="缺失原因或口径说明")


class AblationSection(BaseModel):
    available: bool = Field(..., description="A/B 汇总是否可用")
    rows: list[AblationRow] = Field(default_factory=list, description="各模式一行")
    note: str = Field(default="", description="口径或缺失说明")


class TimingSection(BaseModel):
    available: bool = Field(..., description="端到端计时是否可用")
    rows: list[TimingRow] = Field(default_factory=list, description="每次计时一行")
    median_seconds: float | None = Field(default=None, description="成功轮次耗时的中位数")
    note: str = Field(default="", description="口径或缺失说明")


class TripStatsSection(BaseModel):
    """等价于 GET /trip/stats 的聚合值（复用服务层，不做 HTTP 自调用）。"""

    available: bool = Field(..., description="行程 token 统计是否可用")
    trip_count: int = Field(default=0, ge=0, description="统计行程数")
    total_prompt_tokens: int = Field(default=0, ge=0, description="总输入 token")
    total_completion_tokens: int = Field(default=0, ge=0, description="总输出 token")
    total_tokens: int = Field(default=0, ge=0, description="总 token")
    item_count: int = Field(default=0, ge=0, description="明细条数")
    note: str = Field(default="", description="口径或缺失说明")


class MetricsDashboardResponse(BaseModel):
    """GET /metrics/dashboard 的完整响应。"""

    status: str = Field(
        ...,
        description="ok=三段齐全 / partial=部分可用 / empty=全部缺失",
    )
    sources: list[DataSourceInfo] = Field(default_factory=list, description="各数据源状态")
    ablation: AblationSection = Field(..., description="RAG 消融 A/B 汇总")
    timing: TimingSection = Field(..., description="端到端计时")
    trip_stats: TripStatsSection = Field(..., description="已保存行程 token 统计")
    notes: list[str] = Field(default_factory=list, description="口径提醒，防止误读数字")
