#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5a · 生成质量评估体系（纯离线规则打分器，不调模型 / 不联网）。

背景
----
项目此前只有检索侧评估（scripts/evaluate_rag_retrieval.py，18 条样例测 top1/MRR），
「只评检索不评生成」是已知短板。本脚本补生成侧：读入一批
「TripRequest + Itinerary」样本，用 5 条**确定性规则**逐条打分，输出汇总（含中位数）。

5 个指标（每项 0.0~1.0，总分 = 5 项等权平均 × 100）
---------------------------------------------------
1. days_complete      天数完整性：实际天数 == (end_date - start_date + 1)，且 day_index 连续 1..N
2. budget_ok          预算不超：estimated_budget <= request.budget（且非负）
3. spots_ok           景点数达标：每天景点数 >= --min-spots 的天数占比
4. weather_tip_ok     天气提示一致性：**后端生成结果的 tips 不得写死天气提示**
5. preference_hit     偏好关键词命中：preferences 在行程全文中的命中比例

指标 4 的口径（重要，别判反）
-----------------------------
源码事实：天气**不在** POST /trip/generate 主链路里；前端 Result.vue 独立调
GET /weather/forecast，再按是否有雨动态增删 itinerary.tips
（rainWeatherKeywords / sunnyTipKeywords，雨天追加「带伞」「防滑鞋」两条）。
因此后端生成的 tips 里出现「雨 / 伞 / 防晒 / 日照」这类硬编码，属于
**越权写死**，会与前端动态注入重复或冲突 → 判 0 分。
本指标只检查「有没有遵守架构约定」，不判断天气建议本身对不对，也不调用天气接口。
若将来评估对象是「前端合并后的展示结果」，判定口径要反过来，需另行拍板。

设计边界（拍板 2026-11-08 18:27）
---------------------------------
* 只读、纯离线：只用标准库，零 app.* import、零网络、零模型调用、零数据库。
* 不改动产品主链路、不改 retriever.py、不碰前端。
* 依赖一份离线评估集 backend/eval/generation_eval_cases.json（手工构造样例，
  仅用于验证规则逻辑，**不是真实模型输出，不代表任何生成质量结论**）。
  评估真实结果请用 --input 指向另存的 JSON。
* 规则打分 ≠ LLM 裁判：只测「结构 / 约束 / 关键词」，不测文笔、事实正确性、
  行程合理性。这是可复现、零成本的基线，不是质量天花板。

用法（PowerShell，解释器一律 backend\\.venv\\Scripts\\python.exe）
------------------------------------------------------------------
  & "D:\\zhilv-yuntu\\backend\\.venv\\Scripts\\python.exe" backend\\scripts\\evaluate_generation_quality.py --help
  & "...python.exe" backend\\scripts\\evaluate_generation_quality.py --self-test
  & "...python.exe" backend\\scripts\\evaluate_generation_quality.py
  & "...python.exe" backend\\scripts\\evaluate_generation_quality.py --input <file.json> --out <report.md>

PowerShell 5.1 中文管道会乱码：stdout 只打 ASCII（英文标签 + 数字）；
中文逐条报告用 --out 由脚本自己写 UTF-8 文件，不经 PowerShell 编码层。

输入 JSON 支持两种形态：
  {"cases": [ {id, request{...}, itinerary{...}}, ... ]}
  或 [ {id, request{...}, itinerary{...}}, ... ]
缺字段按 0 分 / 跳过处理，不抛异常。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DEFAULT_CASES = BACKEND_DIR / "eval" / "generation_eval_cases.json"

METRIC_KEYS = (
    "days_complete",
    "budget_ok",
    "spots_ok",
    "weather_tip_ok",
    "preference_hit",
)

METRIC_LABELS = {
    "days_complete": "天数完整性",
    "budget_ok": "预算不超",
    "spots_ok": "景点数达标",
    "weather_tip_ok": "天气提示一致性",
    "preference_hit": "偏好关键词命中",
}

# 对齐 frontend/src/views/Result.vue:93-94 的两组关键词，并补上前端雨天
# 动态注入的两条提示里的关键词（108-109 行）。命中即视为后端越权写死天气。
WEATHER_HARDCODE_KEYWORDS = (
    "雨", "雷阵雨", "阵雨", "小雨", "中雨", "大雨", "暴雨",
    "伞", "雨衣", "湿滑", "防滑",
    "防晒", "太阳", "日照", "晒",
)


# ------------------------------------------------------------------ 输入解析
def load_cases(path: Path) -> list[dict[str, Any]]:
    """读取评估集；兼容 {"cases": [...]} 与裸 list 两种形态。"""
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        cases = data.get("cases", [])
    elif isinstance(data, list):
        cases = data
    else:
        raise ValueError("评估集必须是 JSON list 或含 cases 键的 object。")
    if not isinstance(cases, list):
        raise ValueError("cases 必须是 list。")
    return [c for c in cases if isinstance(c, dict)]


# ------------------------------------------------------------------ 工具函数
def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _to_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _itinerary_text(itinerary: dict[str, Any]) -> str:
    """把行程里所有可检索文本拼成一块，供偏好关键词命中判定。"""
    parts: list[str] = [str(itinerary.get("summary") or "")]
    for day in _as_list(itinerary.get("days")):
        if not isinstance(day, dict):
            continue
        parts.append(str(day.get("theme") or ""))
        for note in _as_list(day.get("notes")):
            parts.append(str(note))
        for key in ("spots", "meals", "transport"):
            for item in _as_list(day.get(key)):
                if not isinstance(item, dict):
                    continue
                for field in ("name", "description", "location", "notes", "mode"):
                    parts.append(str(item.get(field) or ""))
        hotel = _as_dict(day.get("hotel"))
        parts.append(str(hotel.get("name") or ""))
    for key in ("tips", "source_notes"):
        for item in _as_list(itinerary.get(key)):
            parts.append(str(item))
    parts.append(str(itinerary.get("destination") or ""))
    return "\n".join(p for p in parts if p)


# ------------------------------------------------------------------ 5 个指标
def score_days_complete(case: dict[str, Any]) -> tuple[float, str]:
    request = _as_dict(case.get("request"))
    itinerary = _as_dict(case.get("itinerary"))
    start, end = _to_date(request.get("start_date")), _to_date(request.get("end_date"))
    days = [d for d in _as_list(itinerary.get("days")) if isinstance(d, dict)]
    if start is None or end is None:
        return 0.0, "缺少合法 start_date/end_date"
    expected = (end - start).days + 1
    if expected <= 0:
        return 0.0, f"日期区间非法 expected={expected}"
    actual = len(days)
    indices = [d.get("day_index") for d in days]
    continuous = indices == list(range(1, actual + 1))
    ok = actual == expected and continuous
    return (1.0 if ok else 0.0), (
        f"expected={expected} actual={actual} day_index_continuous={continuous}"
    )


def score_budget_ok(case: dict[str, Any]) -> tuple[float, str]:
    request = _as_dict(case.get("request"))
    itinerary = _as_dict(case.get("itinerary"))
    limit = _to_float(request.get("budget"))
    estimated = _to_float(itinerary.get("estimated_budget"))
    if limit is None or estimated is None:
        return 0.0, "缺少 budget 或 estimated_budget"
    ok = 0 <= estimated <= limit
    return (1.0 if ok else 0.0), f"estimated={estimated:g} limit={limit:g}"


def score_spots_ok(case: dict[str, Any], min_spots: int) -> tuple[float, str]:
    itinerary = _as_dict(case.get("itinerary"))
    days = [d for d in _as_list(itinerary.get("days")) if isinstance(d, dict)]
    if not days:
        return 0.0, "days 为空"
    counts = [len(_as_list(d.get("spots"))) for d in days]
    qualified = sum(1 for n in counts if n >= min_spots)
    return qualified / len(days), f"per_day={counts} min_spots={min_spots}"


def score_weather_tip_ok(case: dict[str, Any]) -> tuple[float, str]:
    itinerary = _as_dict(case.get("itinerary"))
    tips = [str(t) for t in _as_list(itinerary.get("tips"))]
    hits = sorted({kw for kw in WEATHER_HARDCODE_KEYWORDS if any(kw in t for t in tips)})
    if hits:
        return 0.0, "后端 tips 写死天气关键词: " + "/".join(hits)
    return 1.0, "后端 tips 无天气硬编码（天气交给独立接口 + 前端动态注入）"


def score_preference_hit(case: dict[str, Any]) -> tuple[float, str]:
    request = _as_dict(case.get("request"))
    itinerary = _as_dict(case.get("itinerary"))
    prefs = [str(p) for p in _as_list(request.get("preferences")) if str(p).strip()]
    if not prefs:
        return 1.0, "无偏好约束，记为通过"
    text = _itinerary_text(itinerary)
    hits = [p for p in prefs if p in text]
    return len(hits) / len(prefs), f"hit={len(hits)}/{len(prefs)} miss={[p for p in prefs if p not in hits]}"


# ------------------------------------------------------------------ 逐条打分
def score_case(case: dict[str, Any], min_spots: int) -> dict[str, Any]:
    days_score, days_detail = score_days_complete(case)
    budget_score, budget_detail = score_budget_ok(case)
    spots_score, spots_detail = score_spots_ok(case, min_spots)
    weather_score, weather_detail = score_weather_tip_ok(case)
    pref_score, pref_detail = score_preference_hit(case)

    scores = {
        "days_complete": days_score,
        "budget_ok": budget_score,
        "spots_ok": spots_score,
        "weather_tip_ok": weather_score,
        "preference_hit": pref_score,
    }
    return {
        "id": str(case.get("id") or case.get("itinerary", {}).get("trip_id") or "<unnamed>"),
        "scores": scores,
        "total": round(sum(scores.values()) / len(scores) * 100, 1),
        "details": {
            "days_complete": days_detail,
            "budget_ok": budget_detail,
            "spots_ok": spots_detail,
            "weather_tip_ok": weather_detail,
            "preference_hit": pref_detail,
        },
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    totals = [r["total"] for r in results]
    per_metric = {
        key: {
            "mean": round(statistics.fmean([r["scores"][key] for r in results]), 3) if results else 0.0,
            "pass_count": sum(1 for r in results if r["scores"][key] >= 1.0),
        }
        for key in METRIC_KEYS
    }
    return {
        "case_count": len(results),
        "per_metric": per_metric,
        "total_mean": round(statistics.fmean(totals), 1) if totals else 0.0,
        "total_median": round(statistics.median(totals), 1) if totals else 0.0,
        "total_min": min(totals) if totals else 0.0,
        "total_max": max(totals) if totals else 0.0,
        "full_score_count": sum(1 for t in totals if t >= 100.0),
    }


# ------------------------------------------------------------------ 输出
def render_ascii(results: list[dict[str, Any]], summary: dict[str, Any], min_spots: int) -> str:
    """stdout 只用 ASCII，避免 PowerShell 5.1 中文管道乱码。"""
    lines: list[str] = []
    lines.append("=== Generation Quality (offline rule-based, NO model call) ===")
    lines.append(f"cases: {summary['case_count']}  min_spots: {min_spots}")
    lines.append("")
    header = (
        f"{'case_id':<32} {'days':>5} {'budget':>7} {'spots':>6} "
        f"{'weather':>8} {'pref':>5} {'total':>7}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for r in results:
        s = r["scores"]
        lines.append(
            f"{r['id'][:32]:<32} {s['days_complete']:>5.1f} {s['budget_ok']:>7.1f} "
            f"{s['spots_ok']:>6.2f} {s['weather_tip_ok']:>8.1f} "
            f"{s['preference_hit']:>5.2f} {r['total']:>7.1f}"
        )
    lines.append("")
    lines.append("=== Summary ===")
    for key in METRIC_KEYS:
        m = summary["per_metric"][key]
        lines.append(
            f"{key:<18} mean={m['mean']:.3f}  pass={m['pass_count']}/{summary['case_count']}"
        )
    lines.append(f"total_mean         {summary['total_mean']:.1f}")
    lines.append(f"total_median       {summary['total_median']:.1f}")
    lines.append(f"total_min          {summary['total_min']:.1f}")
    lines.append(f"total_max          {summary['total_max']:.1f}")
    lines.append(f"full_score_count   {summary['full_score_count']}/{summary['case_count']}")
    lines.append("")
    lines.append("NOTE: scores come from 5 deterministic rules, NOT an LLM judge.")
    lines.append("NOTE: eval cases are OFFLINE HANDCRAFTED samples, NOT real model output.")
    lines.append("NOTE: metric weather_tip_ok checks that backend tips do NOT hardcode weather")
    lines.append("      (weather is a separate GET /weather/forecast + frontend Result.vue).")
    return "\n".join(lines)


def render_markdown(
    results: list[dict[str, Any]], summary: dict[str, Any], min_spots: int, source: Path
) -> str:
    """中文逐条报告，由脚本自己写 UTF-8 文件，不经 PowerShell 编码层。"""
    lines: list[str] = []
    lines.append("# 生成质量评估（5a · 离线规则打分）")
    lines.append("")
    lines.append(f"> 评估集：`{source.as_posix()}`　样例数：{summary['case_count']}　每天最少景点：{min_spots}")
    lines.append("> 纯离线规则打分，**不调用任何模型**；评估集是手工构造样例，**不代表真实模型生成质量**。")
    lines.append("")
    lines.append("## 五个指标")
    lines.append("")
    lines.append("| 指标 | 判定规则 |")
    lines.append("|---|---|")
    lines.append("| 天数完整性 | 实际天数 = 结束日期 − 开始日期 + 1，且 `day_index` 连续 1..N |")
    lines.append("| 预算不超 | `estimated_budget` 非负且 ≤ `request.budget` |")
    lines.append(f"| 景点数达标 | 每天景点数 ≥ {min_spots} 的天数占比（0~1 连续分） |")
    lines.append("| 天气提示一致性 | 后端 `tips` 不含天气硬编码词（雨/伞/防晒/日照…），见下方口径 |")
    lines.append("| 偏好关键词命中 | `preferences` 在行程全文（概述/主题/景点/备注/tips）中的命中比例 |")
    lines.append("")
    lines.append("总分 = 五项等权平均 × 100；规则只测结构与约束，**不测文笔、事实正确性、行程合理性**。")
    lines.append("")
    lines.append("**天气提示一致性为什么这样判**：源码里天气不在 `POST /trip/generate` 链路，前端")
    lines.append("`Result.vue` 独立调 `GET /weather/forecast` 后按是否有雨动态增删 `tips`。")
    lines.append("后端若在 `tips` 写死「带伞 / 防晒」，会与前端注入重复或冲突，属于越权 → 判 0。")
    lines.append("本指标只查架构约定，不判断天气建议对错，也不调用天气接口。")
    lines.append("")
    lines.append("## 逐条打分")
    lines.append("")
    lines.append("| case_id | 天数 | 预算 | 景点 | 天气 | 偏好 | 总分 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        s = r["scores"]
        lines.append(
            f"| `{r['id']}` | {s['days_complete']:.1f} | {s['budget_ok']:.1f} | "
            f"{s['spots_ok']:.2f} | {s['weather_tip_ok']:.1f} | "
            f"{s['preference_hit']:.2f} | **{r['total']:.1f}** |"
        )
    lines.append("")
    lines.append("### 逐条判定依据")
    lines.append("")
    for r in results:
        lines.append(f"- **`{r['id']}`**")
        for key in METRIC_KEYS:
            lines.append(f"  - {METRIC_LABELS[key]}（{r['scores'][key]:.2f}）：{r['details'][key]}")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append("| 统计量 | 值 |")
    lines.append("|---|---|")
    for key in METRIC_KEYS:
        m = summary["per_metric"][key]
        lines.append(
            f"| {METRIC_LABELS[key]}均值 | {m['mean']:.3f}（满分 {m['pass_count']}/{summary['case_count']}） |"
        )
    lines.append(f"| 总分均值 | {summary['total_mean']:.1f} |")
    lines.append(f"| **总分中位数** | **{summary['total_median']:.1f}** |")
    lines.append(f"| 总分最小 / 最大 | {summary['total_min']:.1f} / {summary['total_max']:.1f} |")
    lines.append(f"| 满分条数 | {summary['full_score_count']}/{summary['case_count']} |")
    lines.append("")
    lines.append("## 口径与禁语")
    lines.append("")
    lines.append("- 这些分数来自**离线规则 + 手工样例**，只能证明「评估器逻辑正确」，不能写成「生成质量 88 分」。")
    lines.append("- 想得到真实结论，需把 `/trip/generate` 的实际响应按同样结构另存 JSON 再用 `--input` 评估。")
    lines.append("- 与检索侧 A/B（18 条 × 3 轮）是两套独立指标，不要混着比，也不要与历史 93.3% 比较。")
    lines.append("- 规则打分是零成本可复现的基线，不替代人工验收或 LLM 裁判。")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 自测
def self_test() -> int:
    """内置断言：5 个指标、总分、中位数、缺字段容错；不依赖外部文件。"""
    checks = 0

    def _case(request: dict, itinerary: dict) -> dict:
        return {"id": "t", "request": request, "itinerary": itinerary}

    base_request = {
        "destination": "大理",
        "start_date": "2026-04-10",
        "end_date": "2026-04-12",
        "budget": 3000,
        "preferences": ["自然风景", "美食"],
    }
    base_days = [
        {"day_index": i, "spots": [{"name": "自然风景"}, {"name": "美食"}]}
        for i in (1, 2, 3)
    ]
    base_itinerary = {
        "summary": "自然风景与美食",
        "estimated_budget": 2600,
        "days": base_days,
        "tips": ["提前订房"],
        "source_notes": [],
    }

    full = score_case(_case(base_request, base_itinerary), 2)
    assert full["total"] == 100.0, full
    assert all(v == 1.0 for v in full["scores"].values()), full
    checks += 1

    # 天数不足
    short = score_case(_case(base_request, {**base_itinerary, "days": base_days[:2]}), 2)
    assert short["scores"]["days_complete"] == 0.0, short
    checks += 1

    # day_index 断号
    broken = [dict(d) for d in base_days]
    broken[2]["day_index"] = 9
    assert score_case(_case(base_request, {**base_itinerary, "days": broken}), 2)["scores"]["days_complete"] == 0.0
    checks += 1

    # 预算超支
    over = score_case(_case(base_request, {**base_itinerary, "estimated_budget": 4200}), 2)
    assert over["scores"]["budget_ok"] == 0.0, over
    checks += 1

    # 景点不足：一天只有 1 个 → 2/3
    thin = [dict(d) for d in base_days]
    thin[2]["spots"] = [{"name": "自然风景"}]
    got = score_case(_case(base_request, {**base_itinerary, "days": thin}), 2)
    assert abs(got["scores"]["spots_ok"] - 2 / 3) < 1e-9, got
    checks += 1

    # 天气硬编码
    rainy = score_case(
        _case(base_request, {**base_itinerary, "tips": ["天气可能有雨，建议随身带伞"]}), 2
    )
    assert rainy["scores"]["weather_tip_ok"] == 0.0, rainy
    assert "伞" in rainy["details"]["weather_tip_ok"]
    checks += 1

    # 偏好命中 1/2
    half = score_case(
        _case({**base_request, "preferences": ["自然风景", "滑雪"]}, base_itinerary), 2
    )
    assert half["scores"]["preference_hit"] == 0.5, half
    checks += 1

    # 无偏好 → 通过
    nopref = score_case(_case({**base_request, "preferences": []}, base_itinerary), 2)
    assert nopref["scores"]["preference_hit"] == 1.0, nopref
    checks += 1

    # 中位数与汇总
    res = [
        score_case(_case(base_request, base_itinerary), 2),
        score_case(_case(base_request, {**base_itinerary, "estimated_budget": 4200}), 2),
        score_case(_case(base_request, {**base_itinerary, "tips": ["注意防晒"]}), 2),
    ]
    sm = summarize(res)
    assert sm["case_count"] == 3, sm
    assert sm["total_median"] == 80.0, sm  # 100, 80, 80
    assert sm["per_metric"]["budget_ok"]["pass_count"] == 2, sm
    checks += 1

    # 缺字段 / 空结构不崩：天气无硬编码、无偏好约束两项按「无违规即通过」记 1.0，
    # 其余三项缺数据记 0 → 总分 40。这里只验证「不抛异常 + 分数确定」，不是质量结论。
    empty = score_case({}, 2)
    assert empty["total"] == 40.0, empty
    assert empty["scores"]["weather_tip_ok"] == 1.0, empty
    assert empty["scores"]["preference_hit"] == 1.0, empty
    assert summarize([])["case_count"] == 0
    checks += 1

    print(f"SELF-TEST PASS ({checks} checks)")
    print("  days/budget/spots/weather/pref rules, median, missing-field tolerance: OK")
    return 0


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="5a: offline rule-based generation quality scoring (no model call).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input", type=Path, default=DEFAULT_CASES,
        help=f"评估集 JSON 路径（默认 {DEFAULT_CASES.as_posix()}）",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="把中文逐条报告写成 UTF-8 Markdown 文件；不指定则只打印 ASCII 到 stdout",
    )
    parser.add_argument(
        "--min-spots", type=int, default=2,
        help="每天最少景点数阈值（默认 2）",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="跑内置断言（不读外部文件），验证 5 个指标与中位数后退出",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        return self_test()
    if args.min_spots < 1:
        print("ERROR: --min-spots must be >= 1", file=sys.stderr)
        return 2
    if not args.input.exists():
        print(f"ERROR: input not found: {args.input.as_posix()}", file=sys.stderr)
        return 2
    try:
        cases = load_cases(args.input)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot load cases: {exc}", file=sys.stderr)
        return 2

    results = [score_case(c, args.min_spots) for c in cases]
    summary = summarize(results)
    print(render_ascii(results, summary, args.min_spots))

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            render_markdown(results, summary, args.min_spots, args.input),
            encoding="utf-8",
        )
        print(f"markdown report written: {args.out.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
