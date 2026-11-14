#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5b - retrieval redundancy analysis: parsing and aggregation core (no CLI, no writes).

Imported by analyze_retrieval_redundancy.py. Split into two files so each stays
under the 20 KB pre-commit hook limit; logic is line-for-line the same as the
original docs/tools/ version. Offline only: no model / Amap / vector DB calls.
"""
from __future__ import annotations

import difflib
import re
import statistics
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

MODE_LABELS = {
    "base": "baseline（全开）",
    "norew": "--no-rewrite（关 LLM 改写）",
    "norr": "--no-rerank（关重排）",
}

# 归一化：去首尾空白 → 去开头章节编号 → 去全部空白与标点 → NFKC → 小写
_LEADING_INDEX = re.compile(r"^\s*\d+(?:\.\d+)*\s*[.、]?\s*")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_TITLE_LINE = re.compile(r"(?m)^\s+\d+\.\s*(.+?)\s*$")
_CANDIDATE_COUNT = re.compile(r"candidate_count=(\d+)")
_RERANK_TOKEN_LINE = re.compile(r"(?m)^rerank_prompt_tokens:\s*(\d+)")
_TITLES_BLOCK = re.compile(r"(?ms)^titles:\s*\n(.*?)(?=^-{10,}|\Z)")
_CASE_SPLIT = re.compile(r"(?m)^case:\s*(.+?)\s*$")


def normalize_title(title: str) -> str:
    """归一化标题，仅用于比对，不改变原始日志。"""
    text = unicodedata.normalize("NFKC", title.strip())
    text = _LEADING_INDEX.sub("", text)
    text = _NON_WORD.sub("", text)
    return text.lower()


def is_near_duplicate(a: str, b: str, threshold: float) -> bool:
    """归一化后相同，或字符序列相似度达阈值。a/b 已归一化。"""
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def parse_log(path: Path, near_threshold: float) -> dict[str, Any]:
    """解析单份日志，返回该轮的重复统计与 token 合计。"""
    text = path.read_text(encoding="utf-8", errors="replace")

    name = path.stem  # rag_base_1 / rag_norew_2 / rag_norr_3
    parts = name.split("_")
    mode = parts[1] if len(parts) >= 3 else "unknown"
    round_no = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0

    # 按 case: 行切块；切出来的第一段是 [rerank]/[embedding] 头部日志，丢弃。
    starts = list(_CASE_SPLIT.finditer(text))
    cases: list[dict[str, Any]] = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[match.start():end]
        title_block = _TITLES_BLOCK.search(block)
        titles = _TITLE_LINE.findall(title_block.group(1)) if title_block else []
        token_match = _RERANK_TOKEN_LINE.search(block)
        cases.append(
            {
                "id": match.group(1),
                "titles": titles,
                "rerank_prompt_tokens": int(token_match.group(1)) if token_match else 0,
            }
        )

    chunk_count = sum(len(c["titles"]) for c in cases)
    all_titles = [t for c in cases for t in c["titles"]]
    distinct_titles = sorted(set(all_titles))
    title_freq = Counter(all_titles)

    # 单次请求内重复：同一 case 的 top5 里，与前面片段精确/近重复的条目数
    intra_exact = 0
    intra_near = 0
    for case in cases:
        seen: list[str] = []
        for title in case["titles"]:
            if title in seen:
                intra_exact += 1
            else:
                seen.append(title)
        norm_seen: list[str] = []
        for title in case["titles"]:
            norm = normalize_title(title)
            if any(is_near_duplicate(norm, prev, near_threshold) for prev in norm_seen):
                intra_near += 1
            else:
                norm_seen.append(norm)

    # 跨请求重复：一份日志里 90 个片段，distinct 只算一次
    cross_repeat = chunk_count - len(distinct_titles)

    rerank_tokens = sum(c["rerank_prompt_tokens"] for c in cases)
    candidate_counts = [int(n) for n in _CANDIDATE_COUNT.findall(text)]
    candidate_count = int(statistics.median(candidate_counts)) if candidate_counts else 0

    avg_chunk_tokens = (
        rerank_tokens / (len(cases) * candidate_count)
        if cases and candidate_count
        else 0.0
    )

    return {
        "file": path.name,
        "mode": mode,
        "round": round_no,
        "cases": len(cases),
        "chunks": chunk_count,
        "distinct_chunks": len(distinct_titles),
        "intra_exact_dup": intra_exact,
        "intra_near_dup": intra_near,
        "cross_repeat": cross_repeat,
        "cross_repeat_pct": round(cross_repeat / chunk_count * 100, 1) if chunk_count else 0.0,
        "cases_with_intra_dup": sum(
            1 for c in cases if len(set(c["titles"])) < len(c["titles"])
        ),
        "rerank_prompt_tokens": rerank_tokens,
        "candidate_count": candidate_count,
        "avg_chunk_tokens": round(avg_chunk_tokens, 1),
        "intra_saving_upper": round(intra_near * avg_chunk_tokens, 1),
        "cross_saving_upper": round(cross_repeat * avg_chunk_tokens, 1),
        "saving_upper_pct_of_rerank": round(
            (cross_repeat * avg_chunk_tokens) / rerank_tokens * 100, 1
        ) if rerank_tokens else 0.0,
        "top_repeated": title_freq.most_common(5),
    }


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 mode 三份日志取中位数，另给 ALL 一行。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["mode"], []).append(row)

    numeric_keys = (
        "cases", "chunks", "distinct_chunks", "intra_exact_dup", "intra_near_dup",
        "cross_repeat", "cross_repeat_pct", "cases_with_intra_dup",
        "rerank_prompt_tokens", "candidate_count", "avg_chunk_tokens",
        "intra_saving_upper", "cross_saving_upper", "saving_upper_pct_of_rerank",
    )
    out: list[dict[str, Any]] = []
    for mode in ("base", "norew", "norr"):
        group = grouped.get(mode, [])
        if not group:
            continue
        item: dict[str, Any] = {"file": f"[{mode}] median of {len(group)} rounds", "mode": mode, "round": 0}
        for key in numeric_keys:
            values = [r[key] for r in group]
            item[key] = round(statistics.median(values), 1)
        item["top_repeated"] = group[0]["top_repeated"]
        out.append(item)

    all_item: dict[str, Any] = {"file": "[ALL] median of 9 logs", "mode": "ALL", "round": 0}
    for key in numeric_keys:
        all_item[key] = round(statistics.median([r[key] for r in rows]), 1)
    all_item["top_repeated"] = max(rows, key=lambda r: r["cross_repeat"])["top_repeated"]
    out.append(all_item)
    return out


