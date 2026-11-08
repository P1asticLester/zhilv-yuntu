"""6a · 看板后端只读聚合服务。

职责
----
把三份来源合成一份 ``MetricsDashboardResponse``（全部只读，不写任何文件）：

=========================  ==========================================  ==========
来源                        位置                                        性质
=========================  ==========================================  ==========
RAG 消融 A/B 汇总           backend/data/metrics/ablation_summary.csv   静态快照
端到端 /trip/generate 计时  backend/data/metrics/timing_result.csv      静态快照
已保存行程 token 统计        app.services.storage_service.get_token_stats 实时服务层
=========================  ==========================================  ==========

设计坑（拍板 18:27 明确要求处理）
--------------------------------
``docs/tools/*.csv`` 在 ``docs/`` 里，被 ``.gitignore`` 忽略，干净环境 / CI 读不到。
**本轮选择：主选「静态快照进仓库」** —— 把 2026-11-08 16:49 那次 9/9 全绿的
A/B 汇总与计时结果复制一份到 ``backend/data/metrics/``，接口默认读它，
干净环境也有确定数据。**同时**保留「文件缺失 → 返回明确空状态」作为防御性兜底
（不是二选一的第二票，是容错：任何人删了快照也不会 500 或假装稳定）。
如需临时看本机最新数据，设环境变量 ``METRICS_DATA_DIR`` 指向 ``docs/tools/`` 即可。

口径约束
--------
* 数字**原样透传**，不换算、不派生「快了 N%」这类禁语指标。
* ``avg_latency_ms`` 只量检索阶段，不是端到端；端到端看 timing.seconds。
* ``/trip/stats`` 复用服务层函数，**不做 HTTP 自调用**（避免进程内回环与断网失败）。
* 静态快照是历史批次，不是实时测量；所有口径提醒放在 response.notes。
"""
from __future__ import annotations

import csv
import os
import statistics
from pathlib import Path

from app.models.metrics_schemas import (
    AblationRow,
    AblationSection,
    DataSourceInfo,
    MetricsDashboardResponse,
    TimingRow,
    TimingSection,
    TripStatsSection,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_METRICS_DIR = BACKEND_DIR / "data" / "metrics"
ABLATION_FILENAME = "ablation_summary.csv"
TIMING_FILENAME = "timing_result.csv"

NOTES = [
    "A/B 汇总来自 2026-11-08 16:49 的静态快照（18 条样例 × 3 轮中位数，REDIS_ENABLED=false 冷链路），不是实时测量。",
    "avg_latency_ms 只量 retrieve_travel_guide_chunks（embedding 召回 + rerank），不含 LLM 改写，也不是端到端耗时。",
    "端到端耗时请看 timing.seconds；28.82s 是 3 次的中位数（36.42 / 28.82 / 28.22），首次偏慢为冷启动。",
    "关 rerank 的收益必须用『top1 88.9%→83.3%、MRR 0.928→0.889』一起讲，不能写成『快了 43%』。",
    "历史 93.3% 是 15 条样例时期且模型配置不同，不与本看板的 88.9% 直接比较。",
    "timing 与 ablation 是两批独立实验，不可相减、不可互相推算。",
]


def _metrics_dir() -> Path:
    """允许用 METRICS_DATA_DIR 临时指向 docs/tools/ 看本机最新数据。"""
    override = os.getenv("METRICS_DATA_DIR", "").strip()
    return Path(override) if override else DEFAULT_METRICS_DIR


def _relative_location(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _to_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_int(value: str | None, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ------------------------------------------------------------------ A/B 汇总
def _load_ablation(metrics_dir: Path) -> tuple[AblationSection, DataSourceInfo]:
    path = metrics_dir / ABLATION_FILENAME
    location = _relative_location(path)
    if not path.is_file():
        return (
            AblationSection(
                available=False,
                rows=[],
                note=f"快照缺失：{location}；请从 docs/tools/ 复制或设 METRICS_DATA_DIR。",
            ),
            DataSourceInfo(
                name="ablation", location=location, available=False,
                kind="missing", note="ablation_summary.csv 不存在",
            ),
        )
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = [
                AblationRow(
                    mode=str(row.get("mode", "")).strip(),
                    rounds_ok=_to_int(row.get("rounds_ok")),
                    redis_enabled=_to_bool(row.get("redis_enabled")),
                    cases=_to_int(row.get("cases")),
                    top1_pct=_to_float(row.get("top1_pct")),
                    topk_pct=_to_float(row.get("topk_pct")),
                    mrr=_to_float(row.get("MRR") or row.get("mrr")),
                    avg_latency_ms=_to_float(row.get("avg_latency_ms")),
                    embedding_tokens=_to_float(row.get("embedding_tokens")),
                    rerank_tokens=_to_float(row.get("rerank_tokens")),
                    rewrite_prompt_tokens=_to_float(row.get("rewrite_prompt_tokens")),
                    rewrite_completion_tokens=_to_float(row.get("rewrite_completion_tokens")),
                )
                for row in csv.DictReader(fh)
                if row.get("mode")
            ]
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        return (
            AblationSection(available=False, rows=[], note=f"读取失败：{type(exc).__name__}: {exc}"),
            DataSourceInfo(
                name="ablation", location=location, available=False,
                kind="missing", note=f"读取失败：{type(exc).__name__}",
            ),
        )
    return (
        AblationSection(
            available=bool(rows), rows=rows,
            note=f"共 {len(rows)} 个模式；数值原样取自 {location}。",
        ),
        DataSourceInfo(
            name="ablation", location=location, available=bool(rows),
            kind="snapshot", note="仓库内静态快照（2026-11-08 A/B 批次）",
        ),
    )


# ------------------------------------------------------------------ 端到端计时
def _load_timing(metrics_dir: Path) -> tuple[TimingSection, DataSourceInfo]:
    path = metrics_dir / TIMING_FILENAME
    location = _relative_location(path)
    if not path.is_file():
        return (
            TimingSection(
                available=False, rows=[], median_seconds=None,
                note=f"快照缺失：{location}；请从 docs/tools/ 复制或设 METRICS_DATA_DIR。",
            ),
            DataSourceInfo(
                name="timing", location=location, available=False,
                kind="missing", note="timing_result.csv 不存在",
            ),
        )
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = [
                TimingRow(
                    run=_to_int(row.get("run")),
                    status=_to_int(row.get("status")),
                    seconds=_to_float(row.get("seconds")),
                    trip_id=str(row.get("trip_id", "")).strip(),
                    days=_to_int(row.get("days")),
                    prompt_tokens=_to_int(row.get("prompt_tokens")),
                    completion_tokens=_to_int(row.get("completion_tokens")),
                    planner_out=_to_int(row.get("planner_out")),
                    error=str(row.get("error", "") or "").strip(),
                )
                for row in csv.DictReader(fh)
                if row.get("run")
            ]
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        return (
            TimingSection(
                available=False, rows=[], median_seconds=None,
                note=f"读取失败：{type(exc).__name__}: {exc}",
            ),
            DataSourceInfo(
                name="timing", location=location, available=False,
                kind="missing", note=f"读取失败：{type(exc).__name__}",
            ),
        )
    success_seconds = [r.seconds for r in rows if r.status == 200 and not r.error]
    median = round(statistics.median(success_seconds), 2) if success_seconds else None
    return (
        TimingSection(
            available=bool(rows), rows=rows, median_seconds=median,
            note=f"共 {len(rows)} 次计时，成功 {len(success_seconds)} 次；数值原样取自 {location}。",
        ),
        DataSourceInfo(
            name="timing", location=location, available=bool(rows),
            kind="snapshot", note="仓库内静态快照（2026-11-08 端到端计时）",
        ),
    )


# ------------------------------------------------------------------ /trip/stats
def _load_trip_stats() -> tuple[TripStatsSection, DataSourceInfo]:
    location = "app.services.storage_service.get_token_stats"
    try:
        # 延迟导入：避免在仅读快照时触发 app.config 的 DB 初始化副作用。
        from app.services.storage_service import get_token_stats

        stats = get_token_stats()
        section = TripStatsSection(
            available=True,
            trip_count=stats.trip_count,
            total_prompt_tokens=stats.total_prompt_tokens,
            total_completion_tokens=stats.total_completion_tokens,
            total_tokens=stats.total_tokens,
            item_count=len(stats.items),
            note="等价于 GET /trip/stats，直接复用服务层函数，未做 HTTP 自调用。",
        )
        source = DataSourceInfo(
            name="trip_stats", location=location, available=True,
            kind="live", note="实时读取本地 SQLite 单表，无网络依赖",
        )
        return section, source
    except Exception as exc:  # noqa: BLE001 - 只读接口必须降级而非 500
        section = TripStatsSection(
            available=False,
            note=f"读取失败：{type(exc).__name__}: {exc}",
        )
        source = DataSourceInfo(
            name="trip_stats", location=location, available=False,
            kind="missing", note=f"服务层不可用：{type(exc).__name__}",
        )
        return section, source


# ------------------------------------------------------------------ 聚合入口
def build_dashboard() -> MetricsDashboardResponse:
    """合成三段数据；任何一段缺失都返回明确空状态，不抛异常。"""
    metrics_dir = _metrics_dir()
    ablation, ablation_source = _load_ablation(metrics_dir)
    timing, timing_source = _load_timing(metrics_dir)
    trip_stats, trip_stats_source = _load_trip_stats()

    available_count = sum(
        1 for section in (ablation, timing, trip_stats) if section.available
    )
    status = "ok" if available_count == 3 else ("partial" if available_count else "empty")

    notes = list(NOTES)
    if metrics_dir != DEFAULT_METRICS_DIR:
        notes.append(
            f"当前数据源被 METRICS_DATA_DIR 覆盖为 {_relative_location(metrics_dir)}，"
            "可能不是仓库内快照，数字口径请自行核对。"
        )
    return MetricsDashboardResponse(
        status=status,
        sources=[ablation_source, timing_source, trip_stats_source],
        ablation=ablation,
        timing=timing,
        trip_stats=trip_stats,
        notes=notes,
    )
