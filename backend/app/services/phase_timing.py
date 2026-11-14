"""分阶段耗时打点（只读观测，不改变任何业务行为）。

四段口径与 docs/mastery/03_主链路源码精读.md 一致
---------------------------------------------------
  retrieval    检索与 RAG 上下文（CURATED 路径的 collect_trip_context）
  planner      Planner 生成草稿
  map_enrich   地图补全（CURATED 路径的 enrich_itinerary_with_map_data）
  budget       预算回算（_refresh_budget_breakdown）

DYNAMIC 路径结构上不经过 retrieval / map_enrich（候选池已带坐标），这两段记
0.0 是结构事实，不是漏测。四段并不覆盖整个函数：中间还有纯 Python 组装
DayPlan / tips / source_notes，日志里用 other = total - sum4 单列，
所以口径是 sum4 + other = total，不写成「四段之和 ≈ 端到端」。

默认行为与降级
--------------
* 默认只写一条结构化 logger.info，**不写任何文件**，断网 / 只读目录 /
  Mock 测试下零 IO 副作用。
* 只有显式设置环境变量 PHASE_TIMING_CSV 才追加一行 CSV（utf-8-sig）：
  PowerShell: $env:PHASE_TIMING_CSV = "D:\\zhilv-yuntu\\backend\\scripts\\_local\\phase_timing.csv"
  写目录不存在 / 无权限 / 磁盘满时一律吞掉异常并 debug 记录，绝不影响返回。
* 数字是本机的、走代理的，不能与旧批次 28.82s / 50.17s 跨环境相减，
  也不能写成「优化快了 N%」。只用于同一批次内看各阶段占比。

离线自检（不联网、不需要 .env）
--------------------------------
  & "D:/zhilv-yuntu/backend/.venv/Scripts/python.exe" "D:/zhilv-yuntu/backend/app/services/phase_timing.py" --self-test
"""
from __future__ import annotations

import csv
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PHASES = ("retrieval", "planner", "map_enrich", "budget")
CSV_FIELDS = (
    "ts", "path", "total_s",
    "retrieval_s", "planner_s", "map_enrich_s", "budget_s",
    "sum4_s", "other_s",
    "retrieval_pct", "planner_pct", "map_enrich_pct", "budget_pct", "other_pct",
)


class PhaseTimer:
    """累积四段耗时；measure() 用 try/finally，异常路径也照样记时。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.started = time.perf_counter()
        self.durations: dict[str, float] = {name: 0.0 for name in PHASES}
        self.finished = False

    @contextmanager
    def measure(self, phase: str):
        if phase not in self.durations:
            raise ValueError(f"unknown phase: {phase}")
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.durations[phase] += time.perf_counter() - t0

    def snapshot(self) -> dict[str, Any]:
        total = time.perf_counter() - self.started
        total_s = round(total, 4)
        seconds = {name: round(value, 4) for name, value in self.durations.items()}
        sum4_s = round(sum(seconds.values()), 4)
        # 秒数先各自 round 再相减，保证 total_s = sum4_s + other_s 严格成立；
        # 百分比一律用未 round 的原始耗时算，否则极小样本会算出 >100% 的占比。
        other_s = round(max(total_s - sum4_s, 0.0), 4)
        raw_other = max(total - sum(self.durations.values()), 0.0)
        pct = lambda value: round(100.0 * value / total, 1) if total > 0 else 0.0  # noqa: E731
        return {
            "path": self.path,
            "total_s": total_s,
            "retrieval_s": seconds["retrieval"],
            "planner_s": seconds["planner"],
            "map_enrich_s": seconds["map_enrich"],
            "budget_s": seconds["budget"],
            "sum4_s": sum4_s,
            "other_s": other_s,
            "retrieval_pct": pct(self.durations["retrieval"]),
            "planner_pct": pct(self.durations["planner"]),
            "map_enrich_pct": pct(self.durations["map_enrich"]),
            "budget_pct": pct(self.durations["budget"]),
            "other_pct": pct(raw_other),
        }

    def finish(self) -> dict[str, Any]:
        """日志必达、CSV 可选；任何写文件失败都不向上抛。"""
        if self.finished:
            return self.snapshot()
        self.finished = True
        snap = self.snapshot()
        logger.info(
            "[phase_timing] path=%s total=%.4fs | retrieval=%.4fs(%.1f%%) "
            "planner=%.4fs(%.1f%%) map_enrich=%.4fs(%.1f%%) budget=%.4fs(%.1f%%) "
            "| sum4=%.4fs other=%.4fs(%.1f%%)",
            snap["path"], snap["total_s"],
            snap["retrieval_s"], snap["retrieval_pct"],
            snap["planner_s"], snap["planner_pct"],
            snap["map_enrich_s"], snap["map_enrich_pct"],
            snap["budget_s"], snap["budget_pct"],
            snap["sum4_s"], snap["other_s"], snap["other_pct"],
        )
        csv_path = os.environ.get("PHASE_TIMING_CSV", "").strip()
        if csv_path:
            self._append_csv(csv_path, snap)
        return snap

    @staticmethod
    def _append_csv(csv_path: str, snap: dict[str, Any]) -> None:
        try:
            path = Path(csv_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not path.exists()
            row = {"ts": datetime.now(timezone.utc).isoformat(), **snap}
            with path.open("a", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
                if is_new:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as exc:  # noqa: BLE001 - 观测代码绝不能影响主链路返回
            logger.debug("[phase_timing] csv append skipped: %s", type(exc).__name__)


def self_test() -> int:
    """离线验证：四段累计、占比、sum4 + other = total、CSV 失败不影响返回。"""
    timer = PhaseTimer("self_test")
    for phase in PHASES:
        with timer.measure(phase):
            time.sleep(0.005)
    snap = timer.finish()

    assert all(snap[f"{phase}_s"] >= 0.004 for phase in PHASES), snap
    assert abs(snap["sum4_s"] - sum(snap[f"{p}_s"] for p in PHASES)) < 0.001, snap
    assert abs(snap["total_s"] - (snap["sum4_s"] + snap["other_s"])) < 0.001, snap
    assert 0 <= snap["other_pct"] <= 100, snap
    assert abs(
        sum(snap[f"{p}_pct"] for p in PHASES) + snap["other_pct"] - 100.0
    ) < 1.0, snap

    # 指向一个必然写不了的路径：finish() 仍要正常返回，不抛异常。
    os.environ["PHASE_TIMING_CSV"] = "Z:/definitely/not/writable/phase_timing.csv"
    try:
        bad = PhaseTimer("csv_failure")
        with bad.measure("planner"):
            pass
        result = bad.finish()
        assert result["path"] == "csv_failure", result
    finally:
        os.environ.pop("PHASE_TIMING_CSV", None)

    print("SELF-TEST PASS: 4 phases / pct / sum4+other=total / csv failure isolated")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(self_test())
    print(__doc__)
