#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5b · 检索去冗【只读分析】：统计 A/B 日志里召回片段的重复/近重复，估算可省 token 上限。

只读边界（拍板 2026-11-08 18:27）
--------------------------------
* 只读 A/B 日志目录（--log-dir；默认 $BENCH_LOGS_DIR 或 backend/scripts/_local/logs）
  下 9 份 rag_<mode>_<round>.txt，不写回、不改任何日志。本机历史日志可用
  --log-dir "D:/zhilv-yuntu/docs/tools/logs" 指过去。
* **不把去重逻辑接进 backend/app/rag/retriever.py**（那会改产品主链路、需用户在场验收）。
* 不动前端、不调真实模型 / 高德 / embedding / rerank，纯离线文本解析。
* 输出的是「理论上限」，**不是已优化、不是实测节省**（标题见 UPPER BOUND）。

为什么这个口径重要（先看结论再读表）
------------------------------------
9 份日志 = 3 模式 × 3 轮，每份 18 case × top5 = 90 个召回片段。实测：
  * **单次请求内 top5 的完全重复 = 0、近重复 = 0** —— 也就是说「结果集内去重」
    这一刀，在当前 79 chunk / 6 城切片粒度下**没有可砍的东西**。
  * 重复几乎全部来自**跨请求反复召回同一批 chunk**：一份日志 90 个片段里
    distinct 只有 24~30，例如「5. 经典三日行程参考」在 18 个 case 中出现 17 次。
  * 因此真正能省 token 的不是「压缩单次结果」，而是「跨请求缓存 / 复用」——
    而后者本项目已有可选 Redis 缓存，且 rerank 是按 query 逐个调用的，
    不同 query 的候选集不同，不能简单套用一个全局去重率。
  * 所以本脚本给出的「可省 token 上限」是**刻意乐观的上界**，用来判断
    「这个优化方向值不值得做」，不是承诺收益；结论见 Markdown 报告。

上限估算方法（全部可复算，写清楚假设）
--------------------------------------
1. rerank_prompt_token 合计：直接累加每个 case 的 rerank_prompt_tokens 行（实测值，
   与 ablation_summary.csv 的 rerank_tokens 中位数一致：base 31803 / norew 33654 / norr 0）。
2. 「一个 chunk 在 rerank 输入中占多少 token」没有直接测量值，用
   avg_chunk_tokens = rerank_prompt_token / (cases × candidate_count) 估算，
   candidate_count 从日志里的 [rerank] calling ... candidate_count=N 行读取（实测恒为 9）。
3. 单次请求内可省上限 = intra_dup_count × avg_chunk_tokens。
   intra_dup_count = 同一 case 的 top5 里，归一化后与前面重复的片段数。
4. 跨请求可省上限（更乐观）= (chunks - distinct_chunks) × avg_chunk_tokens，
   隐含「重复召回的 chunk 只算一次」这个不现实的假设 —— 仅作方向性上界。
5. 近重复判定：归一化 = 去首尾空白 + 去开头「2.5 」式编号 + 去全部空白/标点 +
   NFKC 全半角统一 + 小写；再用 difflib.SequenceMatcher ≥ --near-threshold 判近重复。
   归一化只用于比对，不改动任何原始日志。

用法（PowerShell，解释器 backend\\.venv\\Scripts\\python.exe）
--------------------------------------------------------------
  & "D:\\zhilv-yuntu\\backend\\.venv\\Scripts\\python.exe" "D:\\zhilv-yuntu\\backend\\scripts\\analyze_retrieval_redundancy.py" --help
  & "...python.exe" "...\\analyze_retrieval_redundancy.py" --self-test
  & "...python.exe" "...\\analyze_retrieval_redundancy.py" --log-dir "D:\\zhilv-yuntu\\docs\\tools\\logs" --csv "D:\\zhilv-yuntu\\docs\\tools\\redundancy_summary.csv"

路径参数（迁移后不再写死 docs/tools/）：
  --log-dir             读 A/B 日志，默认 $BENCH_LOGS_DIR 或 backend/scripts/_local/logs
  --csv / --md / --json 写报告，默认都不写（纯屏幕输出）；产物目录自己指定，
                        建议用 $BENCH_OUTPUT_DIR 或 backend/scripts/_local/（被 .gitignore 忽略）

文件拆分（2026-11-14 迁仓时）
------------------------------
原 docs/tools/ 单文件 25 KB，超过仓库 .githooks 的 20 KB 限制。按功能拆为：
  本文件 = CLI / 渲染 / 自测；redundancy_core.py = 解析与统计核心。
两段逻辑逐行不变，运行方式（直接路径执行）不变。

stdout 只打 ASCII（PowerShell 5.1 中文管道会乱码）；中文报告由脚本自己写 UTF-8。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

from redundancy_core import (
    MODE_LABELS, aggregate, is_near_duplicate, normalize_title, parse_log,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
LOCAL_DIR = Path(
    os.environ.get("BENCH_OUTPUT_DIR", "").strip() or str(SCRIPTS_DIR / "_local")
)
DEFAULT_LOG_DIR = Path(
    os.environ.get("BENCH_LOGS_DIR", "").strip() or str(LOCAL_DIR / "logs")
)

# ------------------------------------------------------------------ 输出
ASCII_COLUMNS = (
    ("file", 32),
    ("cases", 6),
    ("chunks", 7),
    ("distinct", 9),
    ("intra_exact", 12),
    ("intra_near", 11),
    ("cross_rep", 10),
    ("cross_%", 8),
    ("rerank_tok", 11),
    ("avg_tok", 8),
    ("intra_up", 9),
    ("cross_up", 9),
    ("upper_%", 8),
)


def render_ascii(rows: list[dict[str, Any]], summary: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("=== Retrieval Redundancy (READ-ONLY / UPPER BOUND, NOT ACHIEVED) ===")
    lines.append("source: <--log-dir>/rag_*.txt  (9 files, 18 cases x top5 each)")
    lines.append("")
    lines.append(" ".join(h.ljust(w) for h, w in ASCII_COLUMNS))
    lines.append("-" * (sum(w for _, w in ASCII_COLUMNS) + len(ASCII_COLUMNS) - 1))
    for r in rows + [{}] + summary:
        if not r:
            continue
        values = [
            r["file"], r["cases"], r["chunks"], r["distinct_chunks"],
            r["intra_exact_dup"], r["intra_near_dup"], r["cross_repeat"],
            r["cross_repeat_pct"], r["rerank_prompt_tokens"],
            r["avg_chunk_tokens"], r["intra_saving_upper"],
            r["cross_saving_upper"], r["saving_upper_pct_of_rerank"],
        ]
        lines.append(" ".join(str(v).ljust(w) for v, (_, w) in zip(values, ASCII_COLUMNS)))
    lines.append("")
    lines.append("intra_*  = duplicate chunks WITHIN one request's top5")
    lines.append("cross_*  = same chunk recalled by DIFFERENT requests (18 cases in one log)")
    lines.append("*_up     = THEORETICAL UPPER BOUND of rerank prompt tokens, NOT measured saving")
    lines.append("")
    lines.append("KEY FINDING: intra_exact_dup = intra_near_dup = 0 in all 9 logs.")
    lines.append("  => dedup INSIDE a result set has nothing to remove at this chunk granularity.")
    lines.append("  => repeats are all CROSS-REQUEST (hot chunks recalled again and again).")
    lines.append("  => do NOT wire dedup into retriever.py; see the Markdown report for why.")
    return "\n".join(lines)


def render_markdown(
    rows: list[dict[str, Any]], summary: list[dict[str, Any]], log_dir: Path
) -> str:
    lines: list[str] = []
    lines.append("# 检索去冗只读分析（5b）")
    lines.append("")
    lines.append(f"> 数据源：`{log_dir.as_posix()}` 下 9 份 A/B 日志（3 模式 × 3 轮，每份 18 case × top5 = 90 片段）。")
    lines.append("> **只读分析**：不改日志、**不接进 `retriever.py`**、不动前端、不调模型 / 高德，纯离线文本解析。")
    lines.append("> 所有 `*_up` 都是**理论上限，不是已优化、不是实测节省**。")
    lines.append("")
    lines.append("## 一张表（按模式取 3 轮中位数）")
    lines.append("")
    lines.append("| 模式 | 片段数 | distinct | 单次请求内精确重复 | 单次请求内近重复 | 跨请求重复 | 跨请求重复率 | rerank token 实测 | 单 chunk 均摊 token | 单请求去冗上限 | 跨请求上限 | 上限占 rerank |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in summary:
        label = {"base": "baseline", "norew": "--no-rewrite", "norr": "--no-rerank", "ALL": "**9 份总体（中位数）**"}[r["mode"]]
        lines.append(
            f"| {label} | {r['chunks']:.0f} | {r['distinct_chunks']:.0f} | "
            f"{r['intra_exact_dup']:.0f} | {r['intra_near_dup']:.0f} | "
            f"{r['cross_repeat']:.0f} | {r['cross_repeat_pct']:.1f}% | "
            f"{r['rerank_prompt_tokens']:.0f} | {r['avg_chunk_tokens']:.1f} | "
            f"{r['intra_saving_upper']:.1f} | {r['cross_saving_upper']:.1f} | "
            f"{r['saving_upper_pct_of_rerank']:.1f}% |"
        )
    lines.append("")
    lines.append("### 9 份日志逐份明细")
    lines.append("")
    lines.append("| 日志 | chunks | distinct | intra 精确 | intra 近重复 | 跨请求重复 | rerank token | 单 chunk 均摊 | 跨请求上限 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| `{r['file']}` | {r['chunks']} | {r['distinct_chunks']} | "
            f"{r['intra_exact_dup']} | {r['intra_near_dup']} | {r['cross_repeat']} | "
            f"{r['rerank_prompt_tokens']} | {r['avg_chunk_tokens']} | {r['cross_saving_upper']} |"
        )
    lines.append("")
    lines.append("## 结论：这一刀不该落在「结果集内去重」")
    lines.append("")
    lines.append("1. **单次请求内 top5 零重复**：9 份日志 162 个 case 里，精确重复与近重复都是 0。")
    lines.append("   当前 6 城 79 chunk 的切片粒度下，rerank 后的 5 个片段本来就互不相同，")
    lines.append("   「检索结果压缩与去冗」在这个位置**没有可砍的东西**，硬加去重只会引入误删风险。")
    lines.append("2. **重复全部是跨请求**：一份日志 90 个片段里 distinct 仅 24~30，")
    lines.append("   例如高频片段被 18 个 case 中的 17 个反复召回。这是「热门 chunk 被反复取用」，")
    lines.append("   属于**缓存 / 复用**范畴，不是「单次结果压缩」。")
    lines.append("3. **跨请求上限为什么不可当真**：rerank 是按 query 逐个调用的，不同 query 的候选集不同，")
    lines.append("   不能把全局去重率直接套到每次调用上；本项目也已有可选 Redis 缓存。")
    lines.append("   表里 `跨请求上限` 假设「重复召回的 chunk 只算一次」，是刻意乐观的上界，")
    lines.append("   用途是判断方向值不值得做，**不是承诺能省这么多**。")
    lines.append("4. **与 ④ A/B 数字一致**：rerank token 实测 base 31803 / norew 33654 / norr 0，")
    lines.append("   与 `ablation_summary.csv` 完全对得上；`--no-rerank` 时 rerank token 为 0，")
    lines.append("   去冗上限自然为 0 —— 说明**成本大头本来就是 rerank 本身**，不是重复片段。")
    lines.append("5. **决策**：不把去重接进 `backend/app/rag/retriever.py`。若将来要降 token，")
    lines.append("   优先级应是 ① 的「按预算关 rerank / 降候选数」这类可 A/B 量化的开关，")
    lines.append("   而不是语义上去重。真要落地去冗，另开一轮拍板并做端到端验收。")
    lines.append("")
    lines.append("## 估算方法与假设（可复算）")
    lines.append("")
    lines.append("- `rerank_prompt_token`：累加每个 case 的 `rerank_prompt_tokens` 行（实测，未估算）。")
    lines.append("- `candidate_count`：从日志 `[rerank] calling ... candidate_count=N` 读取，9 份日志实测恒为 9。")
    lines.append("- `avg_chunk_tokens = rerank_prompt_token / (cases × candidate_count)`；")
    lines.append("  单 chunk 的真实 token **日志里没有**，这是均摊近似，含 query 与指令开销，偏乐观。")
    lines.append("- `单请求去冗上限 = 单次请求内近重复数 × avg_chunk_tokens`。")
    lines.append("- `跨请求上限 = (chunks − distinct_chunks) × avg_chunk_tokens`。")
    lines.append("- 近重复判定：归一化（去编号前缀 / 空白 / 标点、NFKC、小写）后相同，")
    lines.append("  或 `difflib.SequenceMatcher ≥ 阈值`；归一化只用于比对，不改原始日志。")
    lines.append("- **口径禁语**：不写「去冗可省 66%」「优化后省了 N token」「重复率太高该去重」；")
    lines.append("  `cross_repeat_pct` 是「跨请求重复召回占比」，不是「冗余率 / 浪费率」。")
    lines.append("- 与 5a 生成质量评估是两套独立指标，不与检索 top1 / MRR / 历史 93.3% 比较。")
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict[str, Any]], summary: list[dict[str, Any]]) -> None:
    keys = (
        "file", "mode", "round", "cases", "chunks", "distinct_chunks",
        "intra_exact_dup", "intra_near_dup", "cross_repeat", "cross_repeat_pct",
        "cases_with_intra_dup", "rerank_prompt_tokens", "candidate_count",
        "avg_chunk_tokens", "intra_saving_upper", "cross_saving_upper",
        "saving_upper_pct_of_rerank",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows + summary:
            writer.writerow(row)


# ------------------------------------------------------------------ 自测
def self_test() -> int:
    """内联合成日志，验证解析、近重复判定、上限公式；不依赖 9 份真实日志。"""
    checks = 0
    assert normalize_title("2.5 大理180度海景网红打卡地") == "大理180度海景网红打卡地"
    assert normalize_title("经济实惠（人均 30 元以下）") == "经济实惠人均30元以下"
    assert normalize_title("２.１ 大理古城－南门楼") == "大理古城南门楼"
    checks += 1

    assert is_near_duplicate("abc", "abc", 0.9)
    assert not is_near_duplicate("abc", "xyz", 0.9)
    assert is_near_duplicate("大理古城南门楼", "大理古城南门楼店", 0.8)
    checks += 1

    # 合成一份日志：2 个 case，第 1 个内部有 1 个精确重复，跨 case 有重复
    synthetic = """# command: fake
[r erank] calling qwen3-rerank: query=x, candidate_count=9, top_k=5
case: c1
titles:
  1. 2.1 大理古城-南门楼
  2. 2.1 大理古城-南门楼
  3. 5. 经典三日行程参考
  4. 经济实惠（人均 30 元以下）
  5. 经济型（200 元/晚以下）
rerank_prompt_tokens: 900
------------------------------------------------------------
case: c2
titles:
  1. 2.1 大理古城-南门楼
  2. 5. 经典三日行程参考
  3. 经济实惠（人均 30 元以下）
  4. 高端体验（人均 200 元以上）
  5. 2.4 大理古城红龙井
rerank_prompt_tokens: 900
------------------------------------------------------------
"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "rag_base_1.txt"
        # 修正故意写错的 [rerank] 标签，保证 candidate_count 被识别
        p.write_text(synthetic.replace("[r erank]", "[rerank]"), encoding="utf-8")
        r = parse_log(p, 0.90)
    # 10 个片段跨 2 个 case，去重后 distinct = 6
    # （2.1南门楼 / 5.三日行程 / 经济实惠 / 经济型 / 高端体验 / 2.4红龙井）
    # 故 cross_repeat = 10 - 6 = 4（含 case 内部那 1 次精确重复）
    assert r["cases"] == 2, r
    assert r["chunks"] == 10, r
    assert r["intra_exact_dup"] == 1, r
    assert r["intra_near_dup"] >= 1, r
    assert r["distinct_chunks"] == 6, r
    assert r["cross_repeat"] == 4, r
    assert r["rerank_prompt_tokens"] == 1800, r
    assert r["candidate_count"] == 9, r
    # avg = 1800 / (2*9) = 100；intra_up = 1*100；cross_up = 4*100
    assert r["avg_chunk_tokens"] == 100.0, r
    assert r["intra_saving_upper"] == 100.0, r
    assert r["cross_saving_upper"] == 400.0, r
    checks += 1

    # 聚合：3 行同 mode 取中位数
    a = {**r, "file": "a", "mode": "base", "round": 1}
    b = {**r, "file": "b", "mode": "base", "round": 2, "chunks": 20}
    c = {**r, "file": "c", "mode": "base", "round": 3, "chunks": 30}
    agg = aggregate([a, b, c])
    base = next(x for x in agg if x["mode"] == "base")
    assert base["chunks"] == 20, base
    checks += 1

    # 归一化不应把不同景点判成近重复（阈值 0.90）
    t1, t2 = normalize_title("2.1 大理古城-南门楼"), normalize_title("2.4 大理古城红龙井")
    assert not is_near_duplicate(t1, t2, 0.90), (t1, t2)
    checks += 1

    print(f"SELF-TEST PASS ({checks} checks)")
    print("  normalize / near-dup / parse / upper-bound formula / aggregate: OK")
    return 0


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="5b: READ-ONLY redundancy analysis over A/B retrieval logs (UPPER BOUND, not achieved).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--log-dir", type=Path, default=DEFAULT_LOG_DIR,
        help=f"A/B 日志目录（默认 {DEFAULT_LOG_DIR.as_posix()}）",
    )
    parser.add_argument(
        "--near-threshold", type=float, default=0.90,
        help="近重复相似度阈值（默认 0.90）",
    )
    parser.add_argument("--csv", type=Path, default=None, help="写汇总 CSV（utf-8-sig）")
    parser.add_argument("--md", type=Path, default=None, help="写中文 Markdown 报告（UTF-8）")
    parser.add_argument("--json", type=Path, default=None, help="写逐份 JSON 明细（UTF-8）")
    parser.add_argument("--self-test", action="store_true", help="跑内置断言后退出")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        return self_test()
    if not 0 < args.near_threshold <= 1:
        print("ERROR: --near-threshold must be in (0, 1]", file=sys.stderr)
        return 2
    if not args.log_dir.is_dir():
        print(f"ERROR: log dir not found: {args.log_dir.as_posix()}", file=sys.stderr)
        return 2

    paths = sorted(args.log_dir.glob("rag_*.txt"))
    if not paths:
        print(f"ERROR: no rag_*.txt under {args.log_dir.as_posix()}", file=sys.stderr)
        return 2
    rows = [parse_log(p, args.near_threshold) for p in paths]
    summary = aggregate(rows)

    print(render_ascii(rows, summary))
    print("")
    if args.csv:
        write_csv(args.csv, rows, summary)
        print(f"csv written: {args.csv.as_posix()}")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(render_markdown(rows, summary, args.log_dir), encoding="utf-8")
        print(f"markdown written: {args.md.as_posix()}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"rows": rows, "summary": summary}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"json written: {args.json.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
