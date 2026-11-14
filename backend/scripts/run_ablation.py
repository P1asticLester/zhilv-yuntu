#!/usr/bin/env python3
"""RAG 消融 A/B 批处理（仓库内基准工具，位于 backend/scripts/）。

一条命令跑完 3 模式 x 3 轮，自己用 UTF-8 写文件，绕开 PowerShell 5.1 的
GB2312 管道解码问题（背景见 .workbuddy/memory/2026-11-08.md）。

用法（PowerShell，解释器用 backend/.venv）：
    & "D:/zhilv-yuntu/backend/.venv/Scripts/python.exe" "D:/zhilv-yuntu/backend/scripts/run_ablation.py" --help
    & "...python.exe" ".../run_ablation.py" --rounds 3
    & "...python.exe" ".../run_ablation.py" --dry-run
    & "...python.exe" ".../run_ablation.py" --self-test
    & "...python.exe" ".../run_ablation.py" --log-dir "D:/zhilv-yuntu/docs/tools/logs"

路径参数（迁移后全部可配，不再写死 docs/tools/）：
    --out-dir     汇总 CSV/MD 目录，默认 $BENCH_OUTPUT_DIR 或 <脚本目录>/_local
    --log-dir     原始日志目录，默认 $BENCH_LOGS_DIR 或 <out-dir>/logs
    BENCH_OUTPUT_DIR / BENCH_LOGS_DIR 环境变量同名覆盖

产物（全部 UTF-8，默认落在 backend/scripts/_local/，被 .gitignore 忽略）：
    <log-dir>/rag_<mode>_<round>.txt   每次运行的完整原始输出
    <out-dir>/ablation_summary.csv     汇总表（utf-8-sig，Excel 可直接开）
    <out-dir>/ablation_summary.md      中文汇总表

快照边界：backend/data/metrics/ablation_summary.csv 是 6a 看板读的进仓静态快照，
本脚本默认不覆盖它；确需刷新时先备份，再显式 --out-dir 指过去，保持 BOM 与
逐字节口径，并在 CHANGELOG 标注样本数与环境。

设计要点：
- 子进程用 sys.executable，保证与父进程同一个解释器（本机默认 python 只有 pip）。
- 给子进程设 PYTHONIOENCODING=utf-8 / PYTHONUTF8=1，强制 UTF-8 输出。
- 屏幕只打印 ASCII，避免 PowerShell 控制台再把中文显示成乱码。
- 汇总按「每个模式各轮取中位数」，不拿单次结果下结论。
- 中途 Ctrl+C 会用已跑完的轮次照常汇总，不白跑。
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
EVAL_SCRIPT = BACKEND_DIR / "scripts" / "evaluate_rag_retrieval.py"


def _env_dir(name: str, fallback: Path) -> Path:
    """环境变量指到哪就读/写到哪；没设就用本地 _local 目录。"""
    value = os.environ.get(name, "").strip()
    return Path(value) if value else fallback


DEFAULT_OUT_DIR = _env_dir("BENCH_OUTPUT_DIR", SCRIPTS_DIR / "_local")
DEFAULT_LOG_DIR = _env_dir("BENCH_LOGS_DIR", DEFAULT_OUT_DIR / "logs")

# 下面三个由 main() 按 --out-dir / --log-dir 重新赋值；保留模块级名字，
# 是为了不改动 write_csv / write_md / 打印这些函数的签名。
OUT_DIR = DEFAULT_OUT_DIR
LOG_DIR = DEFAULT_LOG_DIR
CSV_PATH = OUT_DIR / "ablation_summary.csv"
MD_PATH = OUT_DIR / "ablation_summary.md"

# (短名, 传给评估脚本的参数)
MODES: list[tuple[str, list[str]]] = [
    ("base", []),
    ("norew", ["--no-rewrite"]),
    ("norr", ["--no-rerank"]),
]

SUMMARY_KEYS = [
    "mode", "redis_enabled", "cases",
    "top1_title_hit_rate", "topk_title_hit_rate", "MRR",
    "avg_latency_ms", "embedding_prompt_tokens_total",
    "rerank_prompt_tokens_total", "rewrite_prompt_tokens_total",
    "rewrite_completion_tokens_total", "noise_rate",
    "cross_destination_pollution",
]

# (汇总列名, Summary 里的键, 解析方式)
MEDIAN_FIELDS = [
    ("top1_pct", "top1_title_hit_rate", "pct"),
    ("topk_pct", "topk_title_hit_rate", "pct"),
    ("MRR", "MRR", "num"),
    ("avg_latency_ms", "avg_latency_ms", "num"),
    ("embedding_tokens", "embedding_prompt_tokens_total", "num"),
    ("rerank_tokens", "rerank_prompt_tokens_total", "num"),
    ("rewrite_prompt_tokens", "rewrite_prompt_tokens_total", "num"),
    ("rewrite_completion_tokens", "rewrite_completion_tokens_total", "num"),
]

MODE_LABELS = {
    "base": "baseline（全开）",
    "norew": "--no-rewrite（关 LLM 改写）",
    "norr": "--no-rerank（关重排）",
}


def ascii_safe(text: str, limit: int = 90) -> str:
    """屏幕输出只留 ASCII，避免 PowerShell 控制台把中文显示成乱码。"""
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)
    return cleaned[:limit]


def parse_summary(text: str) -> dict[str, str]:
    """从评估脚本的 stdout 里抓 Summary 段的键值行（全是 ASCII 键）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        for key in SUMMARY_KEYS:
            if line.startswith(key + ":"):
                out[key] = line.split(":", 1)[1].strip()
                break
    return out


def as_number(value: str, kind: str) -> float | None:
    if kind == "pct":
        match = re.search(r"\(([\d.]+)%\)", value) or re.search(r"([\d.]+)%", value)
        return float(match.group(1)) if match else None
    try:
        return float(value.split()[0].rstrip("%"))
    except (ValueError, IndexError):
        return None


def build_rows(records: list[dict]) -> list[dict]:
    """每个模式一行，各字段取成功轮次的中位数。"""
    rows = []
    for mode, _ in MODES:
        subset = [r for r in records if r["mode"] == mode and r["ok"]]
        first = subset[0]["summary"] if subset else {}
        row = {
            "mode": mode,
            "rounds_ok": len(subset),
            "redis_enabled": first.get("redis_enabled", ""),
            "cases": first.get("cases", ""),
        }
        for label, key, kind in MEDIAN_FIELDS:
            values = [
                as_number(r["summary"].get(key, ""), kind) for r in subset
            ]
            values = [v for v in values if v is not None]
            row[label] = round(statistics.median(values), 3) if values else ""
        rows.append(row)
    return rows


def write_csv(rows: list[dict]) -> None:
    fields = ["mode", "rounds_ok", "redis_enabled", "cases"] + [
        item[0] for item in MEDIAN_FIELDS
    ]
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_md(rows: list[dict], rounds: int, interrupted: bool) -> None:
    lines = [
        "# RAG 消融 A/B 汇总",
        "",
        f"> 每个模式计划跑 {rounds} 轮，下表取**中位数**；单次结果不作结论。",
        "> 口径：`REDIS_ENABLED=false` 冷链路，同一批评估样例，单变量对照。",
        "> 由 `backend/scripts/run_ablation.py` 自动汇总，原始输出见 "
        f"`{LOG_DIR.as_posix()}`。",
    ]
    if interrupted:
        lines.append("> **注意：本次被中断，部分模式轮次数不足，数字仅供参考。**")
    lines += [
        "",
        "| 模式 | 成功轮数 | top1 命中 | topK 命中 | MRR | 检索耗时(ms) | rerank token | rewrite token |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {label} | {rounds_ok} | {top1_pct}% | {topk_pct}% | {MRR} | "
            "{avg_latency_ms} | {rerank_tokens} | {rewrite_prompt_tokens} |".format(
                label=MODE_LABELS[row["mode"]], **row
            )
        )
    lines += [
        "",
        "## 读法",
        "",
        "- `top1 / MRR` 下降多少 = 被关掉的那个环节贡献了多少检索质量。",
        "- `检索耗时` 与 `rerank token` 下降多少 = 那个环节的成本。",
        "- 关 rewrite 会换一条 query，召回结果也随之变化，属于该环节的连带影响，",
        "  不是额外变量；关 rerank 则只改排序、不改召回。",
        "- 历史数字 93.3% 来自 15 条样例时期且模型配置不同，不与本表直接比较。",
        "",
    ]
    MD_PATH.write_text("\n".join(lines), encoding="utf-8")


def print_ascii_summary(rows: list[dict]) -> None:
    print("\n=== Summary (median per mode; ASCII only on purpose) ===")
    print(
        f"{'mode':<8}{'rounds':>7}{'top1%':>9}{'topk%':>9}{'MRR':>8}"
        f"{'lat_ms':>11}{'rerank_tok':>13}{'rewrite_tok':>13}"
    )
    for row in rows:
        def cell(key: str) -> str:
            value = row[key]
            return f"{value:.2f}" if isinstance(value, float) else str(value)

        print(
            f"{row['mode']:<8}{row['rounds_ok']:>7}{cell('top1_pct'):>9}"
            f"{cell('topk_pct'):>9}{cell('MRR'):>8}{cell('avg_latency_ms'):>11}"
            f"{cell('rerank_tokens'):>13}{cell('rewrite_prompt_tokens'):>13}"
        )


def run_self_test() -> int:
    """用假的 Summary 文本验证解析与中位数逻辑，不联网、不写文件。"""
    def fake(mode: str, top1: float, latency: float, rerank: int) -> dict:
        text = (
            "=== Summary ===\n"
            f"mode: {mode}\n"
            "redis_enabled: false\n"
            "cases: 18\n"
            f"top1_title_hit_rate: 15/18 ({top1}%)\n"
            "topk_title_hit_rate: 18/18 (100.0%)\n"
            "MRR: 0.883\n"
            f"avg_latency_ms: {latency}\n"
            "embedding_prompt_tokens_total: 597\n"
            f"rerank_prompt_tokens_total: {rerank}\n"
            "rewrite_prompt_tokens_total: 2206\n"
            "rewrite_completion_tokens_total: 309\n"
        )
        return {"mode": mode, "round": 0, "ok": True,
                "summary": parse_summary(text), "error": ""}

    records = [
        fake("base", 80.0, 4000.0, 31000),
        fake("base", 83.3, 4184.0, 31872),
        fake("base", 86.7, 4300.0, 32000),
        fake("norew", 90.0, 3900.0, 33000),
        fake("norr", 70.0, 2500.0, 0),
    ]
    rows = build_rows(records)
    by_mode = {r["mode"]: r for r in rows}
    assert by_mode["base"]["rounds_ok"] == 3, "rounds_ok mismatch"
    assert by_mode["base"]["top1_pct"] == 83.3, "median(top1) mismatch"
    assert by_mode["base"]["avg_latency_ms"] == 4184.0, "median(latency) mismatch"
    assert by_mode["base"]["rerank_tokens"] == 31872, "median(rerank) mismatch"
    assert by_mode["norr"]["rerank_tokens"] == 0, "no-rerank token should be 0"
    assert by_mode["norew"]["top1_pct"] == 90.0, "single-round median mismatch"
    print_ascii_summary(rows)
    print("\nSELF-TEST PASS: parse + median + ascii output OK (no network, no files)")
    return 0


def main() -> int:
    global OUT_DIR, LOG_DIR, CSV_PATH, MD_PATH

    parser = argparse.ArgumentParser(
        description="Batch RAG ablation A/B with UTF-8 safe output (PowerShell 5.1)."
    )
    parser.add_argument("--rounds", type=int, default=3,
                        help="Rounds per mode (default: 3).")
    parser.add_argument("--cases", type=Path, default=None,
                        help="Optional custom eval cases JSON path.")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout in seconds for a single run (default: 600).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only print the commands, execute nothing.")
    parser.add_argument("--self-test", action="store_true",
                        help="Check parse/median logic offline, no network.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help="Where to write ablation_summary.csv/md "
                             "(default: $BENCH_OUTPUT_DIR or backend/scripts/_local).")
    parser.add_argument("--log-dir", type=Path, default=None,
                        help="Where to write raw rag_*.txt logs "
                             "(default: $BENCH_LOGS_DIR or <out-dir>/logs).")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if not EVAL_SCRIPT.exists():
        print(f"ERROR: evaluator not found: {EVAL_SCRIPT}")
        return 2
    if args.rounds < 1:
        print("ERROR: --rounds must be >= 1")
        return 2

    OUT_DIR = args.out_dir
    LOG_DIR = args.log_dir or (
        DEFAULT_LOG_DIR if args.out_dir == DEFAULT_OUT_DIR else OUT_DIR / "logs"
    )
    CSV_PATH = OUT_DIR / "ablation_summary.csv"
    MD_PATH = OUT_DIR / "ablation_summary.md"

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    total = args.rounds * len(MODES)
    records: list[dict] = []
    interrupted = False
    index = 0

    if not args.dry_run:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        for round_no in range(1, args.rounds + 1):
            for mode, flags in MODES:
                index += 1
                cmd = [sys.executable, str(EVAL_SCRIPT), *flags]
                if args.cases:
                    cmd += ["--cases", str(args.cases)]
                label = f"[{index}/{total}] {mode} round {round_no}"

                if args.dry_run:
                    print(f"{label} -> {' '.join(cmd)}")
                    continue

                print(f"{label} ... running", flush=True)
                proc = None
                try:
                    proc = subprocess.run(
                        cmd, cwd=str(BACKEND_DIR), env=env,
                        capture_output=True, timeout=args.timeout,
                    )
                    stdout = proc.stdout.decode("utf-8", errors="replace")
                    stderr = proc.stderr.decode("utf-8", errors="replace")
                except subprocess.TimeoutExpired as exc:
                    raw = exc.stdout or b""
                    stdout = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""
                    stderr = f"TIMEOUT after {args.timeout}s"
                    proc = None
                except Exception as exc:  # noqa: BLE001
                    stdout, stderr, proc = "", f"ERROR: {exc}", None

                exit_code = proc.returncode if proc is not None else "FAILED"
                log_path = LOG_DIR / f"rag_{mode}_{round_no}.txt"
                log_path.write_text(
                    "# command: " + " ".join(cmd) + "\n"
                    + f"# exit: {exit_code}\n\n"
                    + stdout
                    + ("\n\n=== STDERR ===\n" + stderr if stderr.strip() else ""),
                    encoding="utf-8",
                )

                summary = parse_summary(stdout)
                ok = bool(proc is not None and proc.returncode == 0 and summary.get("mode"))
                records.append({
                    "mode": mode, "round": round_no, "ok": ok,
                    "summary": summary, "error": stderr[:300],
                })
                status = "OK" if ok else "FAILED"
                extra = ""
                if not ok and stderr.strip():
                    extra = " | " + ascii_safe(stderr.strip().splitlines()[0], 60)
                print(f"{label} ... {status} (exit={exit_code}){extra}", flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted; summarizing finished runs only.", flush=True)

    if args.dry_run:
        print(f"\nDry run only: {total} commands listed, nothing executed, no file written.")
        return 0

    rows = build_rows(records)
    write_csv(rows)
    write_md(rows, args.rounds, interrupted)
    print_ascii_summary(rows)
    print(f"\nUTF-8 logs : {LOG_DIR}")
    print(f"CSV        : {CSV_PATH}")
    print(f"Markdown   : {MD_PATH}")

    failed = [r for r in records if not r["ok"]]
    if failed or interrupted or len(records) < total:
        print("\nNOTE: some runs failed or were interrupted; check logs before citing numbers.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
