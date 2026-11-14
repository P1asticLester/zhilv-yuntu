#!/usr/bin/env python3
"""7b - 跨请求缓存复用 A/B（仓库内基准工具，位于 backend/scripts/）。

与 5b 的 run_ablation.py 分开：产物全部落在独立目录，绝不覆盖那 9 份
`rag_<mode>_<round>.txt` 与 `ablation_summary.csv/md`（5b / 6a 的证据链）。

用法（PowerShell，解释器用 backend/.venv）：
    & "D:/zhilv-yuntu/backend/.venv/Scripts/python.exe" "D:/zhilv-yuntu/backend/scripts/run_cache_ablation.py" --help
    & "...python.exe" ".../run_cache_ablation.py" --self-test
    & "...python.exe" ".../run_cache_ablation.py" --dry-run
    & "...python.exe" ".../run_cache_ablation.py" --redis off --rounds 3
    & "...python.exe" ".../run_cache_ablation.py" --redis on  --rounds 3

路径参数（迁移后全部可配，不再写死 docs/tools/）：
    --out-dir     汇总 CSV/MD 目录，默认 $BENCH_OUTPUT_DIR 或 <脚本目录>/_local
    --log-dir     原始日志目录，默认 $BENCH_LOGS_DIR 或 <out-dir>/logs/cache_ab
    BENCH_OUTPUT_DIR / BENCH_LOGS_DIR 环境变量同名覆盖

产物默认落在 backend/scripts/_local/（被 .gitignore 忽略），不会覆盖
backend/data/metrics/ 的进仓静态快照。

屏幕只打 ASCII；中文汇总由脚本自己写 UTF-8 文件，绕开 PowerShell 5.1
GB2312 管道解码问题（见 .workbuddy/memory/2026-11-08.md）。

设计要点：
- 子进程用 sys.executable，保证同一解释器；环境变量临时覆盖 REDIS_ENABLED，
  不改 backend/.env、不改 config.py 默认值（产品默认仍为 false）。
- 每组跑 3 模式 x N 轮；同一组内**不清 Redis**，让跨请求 / 跨轮复用真实发生。
- 除 token / 耗时外，额外记录每轮的 Redis keyspace_hits / misses 增量，
  用来解释「实测节省」到底来自哪里，而不是只报一个差值。
- 汇总按每个模式各轮取中位数，单次结果不作结论。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

from run_ablation import (
    DEFAULT_OUT_DIR, _env_dir, MEDIAN_FIELDS, MODES, SUMMARY_KEYS,
    ascii_safe, parse_summary,
)


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
EVAL_SCRIPT = BACKEND_DIR / "scripts" / "evaluate_rag_retrieval.py"
DEFAULT_LOG_DIR = _env_dir("BENCH_LOGS_DIR", DEFAULT_OUT_DIR / "logs" / "cache_ab")

# 下面三个由 main() 按 --out-dir / --log-dir 重新赋值；保留模块级名字，
# 是为了不改动 write_csv / write_md / 打印这些函数的签名。
OUT_DIR = DEFAULT_OUT_DIR
LOG_DIR = DEFAULT_LOG_DIR
CSV_PATH = OUT_DIR / "cache_ablation_summary.csv"
MD_PATH = OUT_DIR / "cache_ablation_summary.md"

MODE_LABELS = {
    "base": "baseline（全开）",
    "norew": "--no-rewrite（关 LLM 改写）",
    "norr": "--no-rerank（关重排）",
}

EXTRA_FIELDS = ["redis_hits", "redis_misses", "redis_keys"]


def _redis_stats() -> dict[str, int]:
    """读取 Redis INFO 的命中计数；Redis 不可用时返回全 0，不影响主流程。"""
    try:
        import redis

        client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
            socket_connect_timeout=2,
        )
        info = client.info("stats")
        return {
            "hits": int(info.get("keyspace_hits", 0)),
            "misses": int(info.get("keyspace_misses", 0)),
            "keys": int(client.dbsize()),
        }
    except Exception:
        return {"hits": 0, "misses": 0, "keys": 0}


def as_number(value: str, kind: str) -> float | None:
    if kind == "pct":
        match = re.search(r"\(([\d.]+)%\)", value) or re.search(r"([\d.]+)%", value)
        return float(match.group(1)) if match else None
    try:
        return float(value.split()[0].rstrip("%"))
    except (ValueError, IndexError):
        return None


def build_rows(records: list[dict], redis_on: bool) -> list[dict]:
    """每个模式一行，各字段取成功轮次的中位数。"""
    rows = []
    for mode, _ in MODES:
        subset = [r for r in records if r["mode"] == mode and r["ok"]]
        first = subset[0]["summary"] if subset else {}
        row = {
            "mode": mode,
            "redis": "on" if redis_on else "off",
            "rounds_ok": len(subset),
            "cases": first.get("cases", ""),
            "redis_enabled_reported": first.get("redis_enabled", ""),
        }
        for label, key, kind in MEDIAN_FIELDS:
            values = [as_number(r["summary"].get(key, ""), kind) for r in subset]
            values = [v for v in values if v is not None]
            row[label] = round(statistics.median(values), 3) if values else ""
        for field in EXTRA_FIELDS:
            values = [r.get(field, 0) for r in subset]
            row[field] = round(statistics.median(values), 1) if values else ""
        rows.append(row)
    return rows


CSV_FIELDS = (
    ["mode", "redis", "rounds_ok", "cases", "redis_enabled_reported"]
    + [item[0] for item in MEDIAN_FIELDS]
    + EXTRA_FIELDS
)


def write_csv(all_rows: list[dict]) -> None:
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)


def write_md(all_rows: list[dict], rounds: int, notes: list[str]) -> None:
    lines = [
        "# 7b · 跨请求检索复用 A/B（开 / 关 Redis 缓存）",
        "",
        f"> 每组 3 模式 x {rounds} 轮，各字段取**中位数**；单次结果不作结论。",
        "> 同一批 18 条评估样例；`redis=off` 与 `redis=on` 为单变量对照。",
        "> 环境变量临时覆盖 `REDIS_ENABLED`，`backend/.env` 与 `config.py` 默认值未改。",
        f"> 原始逐轮输出见 `{LOG_DIR.as_posix()}`，逐轮数字见 `raw_<group>.json`。",
        "",
        "| 组 | 模式 | 轮数 | cases | top1% | MRR | 检索耗时(ms) | rerank token | rewrite token | hits | misses | keys |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in all_rows:
        lines.append(
            "| {redis} | {mode} | {rounds_ok} | {cases} | {top1_pct} | {MRR} | "
            "{avg_latency_ms} | {rerank_tokens} | {rewrite_prompt_tokens} | "
            "{redis_hits} | {redis_misses} | {redis_keys} |".format(**row)
        )
    lines += ["", "## 口径与读法", ""]
    lines += [f"- {note}" for note in notes]
    lines += [
        "",
        "- **评估脚本只走 `retrieve_travel_guide_chunks`**：命中的是 `rerank:*` 缓存，",
        "  `rag:guide:*` 那层（在 `retrieve_travel_guide` 里）不在这条链路上，本实验**未覆盖**。",
        "- 5b 的 ≈11779 / ≈13711 是按 `rerank_prompt_tokens` 估算的**刻意乐观理论上限**，",
        "  含 query 开销且假设重复片段全部可省；本表是实测，两者不可直接当同一个数。",
        "- 历史 5b 的 9 份 `rag_*.txt` / `ablation_summary.*` 未被覆盖，可作关缓存基线交叉校验。",
        "",
    ]
    MD_PATH.write_text("\n".join(lines), encoding="utf-8")


def print_ascii_summary(all_rows: list[dict]) -> None:
    print("\n=== 7b summary (median per mode; ASCII only on purpose) ===")
    print(
        f"{'redis':<6}{'mode':<8}{'rounds':>7}{'top1%':>8}{'MRR':>7}"
        f"{'lat_ms':>10}{'rerank_tok':>12}{'rewrite_tok':>13}"
        f"{'hits':>7}{'miss':>7}{'keys':>7}"
    )
    for row in all_rows:
        def cell(key: str) -> str:
            value = row[key]
            return f"{value:.2f}" if isinstance(value, float) else str(value)

        print(
            f"{row['redis']:<6}{row['mode']:<8}{row['rounds_ok']:>7}"
            f"{cell('top1_pct'):>8}{cell('MRR'):>7}{cell('avg_latency_ms'):>10}"
            f"{cell('rerank_tokens'):>12}{cell('rewrite_prompt_tokens'):>13}"
            f"{cell('redis_hits'):>7}{cell('redis_misses'):>7}{cell('redis_keys'):>7}"
        )


def run_self_test() -> int:
    """离线验证解析 / 中位数 / 汇总逻辑，不联网、不写文件。"""
    def fake(mode: str, rerank: int, latency: float, hits: int) -> dict:
        text = (
            "=== Summary ===\n"
            f"mode: {mode}\n"
            "redis_enabled: true\n"
            "cases: 18\n"
            "top1_title_hit_rate: 16/18 (88.9%)\n"
            "topk_title_hit_rate: 18/18 (100.0%)\n"
            "MRR: 0.928\n"
            f"avg_latency_ms: {latency}\n"
            "embedding_prompt_tokens_total: 597\n"
            f"rerank_prompt_tokens_total: {rerank}\n"
            "rewrite_prompt_tokens_total: 2206\n"
            "rewrite_completion_tokens_total: 309\n"
        )
        return {"mode": mode, "round": 0, "ok": True, "summary": parse_summary(text),
                "error": "", "redis_hits": hits, "redis_misses": 2, "redis_keys": 5}

    records = [
        fake("base", 31000, 1400.0, 1),
        fake("base", 31800, 1500.0, 3),
        fake("base", 32000, 1600.0, 5),
        fake("norew", 33654, 1687.0, 4),
        fake("norr", 0, 853.4, 0),
    ]
    rows = build_rows(records, True)
    by_mode = {r["mode"]: r for r in rows}
    assert by_mode["base"]["rounds_ok"] == 3, "rounds_ok mismatch"
    assert by_mode["base"]["rerank_tokens"] == 31800, "median(rerank) mismatch"
    assert by_mode["base"]["avg_latency_ms"] == 1500.0, "median(latency) mismatch"
    assert by_mode["base"]["redis_hits"] == 3, "median(hits) mismatch"
    assert by_mode["norr"]["rerank_tokens"] == 0, "no-rerank token should be 0"
    assert all(r["redis"] == "on" for r in rows), "redis label mismatch"
    print_ascii_summary(rows)
    print("\nSELF-TEST PASS: parse + median + ascii output OK (no network, no files)")
    return 0


def main() -> int:
    global OUT_DIR, LOG_DIR, CSV_PATH, MD_PATH

    parser = argparse.ArgumentParser(
        description="7b cross-request cache reuse A/B, UTF-8 safe output."
    )
    parser.add_argument("--redis", choices=("on", "off"), default="on",
                        help="Set REDIS_ENABLED for child evaluator processes.")
    parser.add_argument("--rounds", type=int, default=3,
                        help="Rounds per mode (default: 3).")
    parser.add_argument("--cases", type=Path, default=None,
                        help="Optional custom eval cases JSON path.")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout in seconds for a single run (default: 600).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only print commands, execute nothing.")
    parser.add_argument("--self-test", action="store_true",
                        help="Check parse/median logic offline, no network.")
    parser.add_argument("--flush", action="store_true",
                        help="FLUSHDB before this group (use once per group start).")
    parser.add_argument("--notes", action="append", default=[],
                        help="Extra note lines written into the Markdown report.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help="Where to write cache_ablation_summary.csv/md "
                             "(default: $BENCH_OUTPUT_DIR or backend/scripts/_local).")
    parser.add_argument("--log-dir", type=Path, default=None,
                        help="Where to write raw logs "
                             "(default: $BENCH_LOGS_DIR or <out-dir>/logs/cache_ab).")
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
        DEFAULT_LOG_DIR if args.out_dir == DEFAULT_OUT_DIR
        else OUT_DIR / "logs" / "cache_ab"
    )
    CSV_PATH = OUT_DIR / "cache_ablation_summary.csv"
    MD_PATH = OUT_DIR / "cache_ablation_summary.md"

    redis_on = args.redis == "on"
    if redis_on and args.flush and not args.dry_run:
        try:
            import redis

            client = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
                decode_responses=True,
                socket_connect_timeout=2,
            )
            client.flushdb()
            print("REDIS_FLUSHDB=OK (group starts from a cold cache)", flush=True)
        except Exception as exc:
            print(f"REDIS_FLUSHDB=FAILED {type(exc).__name__}: {exc}", flush=True)
            return 2

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["REDIS_ENABLED"] = "true" if redis_on else "false"
    env["REDIS_URL"] = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

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
                argv = [sys.executable, str(EVAL_SCRIPT), *flags]
                if args.cases:
                    argv += ["--cases", str(args.cases)]
                label = f"[{index}/{total}] redis={args.redis} {mode} round {round_no}"

                if args.dry_run:
                    print(f"{label} -> {' '.join(argv)}")
                    continue

                before = _redis_stats() if redis_on else {"hits": 0, "misses": 0, "keys": 0}
                print(f"{label} ... running", flush=True)
                proc = None
                try:
                    proc = subprocess.run(
                        argv, cwd=str(BACKEND_DIR), env=env,
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

                after = _redis_stats() if redis_on else {"hits": 0, "misses": 0, "keys": 0}
                exit_code = proc.returncode if proc is not None else "FAILED"
                log_path = LOG_DIR / f"{args.redis}_{mode}_{round_no}.txt"
                log_path.write_text(
                    "# command: " + " ".join(argv) + "\n"
                    + f"# exit: {exit_code}\n"
                    + f"# redis_hits_delta: {after['hits'] - before['hits']}\n"
                    + f"# redis_misses_delta: {after['misses'] - before['misses']}\n"
                    + f"# redis_keys_now: {after['keys']}\n\n"
                    + stdout
                    + ("\n\n=== STDERR ===\n" + stderr if stderr.strip() else ""),
                    encoding="utf-8",
                )

                summary = parse_summary(stdout)
                ok = bool(proc is not None and proc.returncode == 0 and summary.get("mode"))
                records.append({
                    "mode": mode, "round": round_no, "ok": ok,
                    "summary": summary, "error": stderr[:300],
                    "redis_hits": after["hits"] - before["hits"],
                    "redis_misses": after["misses"] - before["misses"],
                    "redis_keys": after["keys"],
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

    rows = build_rows(records, redis_on)

    # 与已有组别的汇总合并：CSV/MD 是 off+on 两组的累积视图。
    all_rows: list[dict] = []
    if CSV_PATH.exists():
        try:
            with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
                for old in csv.DictReader(handle):
                    if old.get("redis") != args.redis:
                        all_rows.append(old)
        except Exception:
            all_rows = []
    all_rows.extend(rows)
    all_rows.sort(key=lambda r: (r.get("redis", ""), r.get("mode", "")))

    write_csv(all_rows)
    notes = list(args.notes)
    if interrupted:
        notes.append("本次被中断，部分模式轮次数不足，数字仅供参考。")
    write_md(all_rows, args.rounds, notes)

    raw_json_path = LOG_DIR / f"raw_{args.redis}.json"
    raw_json_path.write_text(
        json.dumps({"group": args.redis, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print_ascii_summary(all_rows)
    print(f"\nUTF-8 logs : {LOG_DIR}")
    print(f"CSV        : {CSV_PATH}")
    print(f"Markdown   : {MD_PATH}")
    print(f"Raw JSON   : {raw_json_path}")

    failed = [r for r in records if not r["ok"]]
    if failed or interrupted or len(records) < total:
        print("\nNOTE: some runs failed or were interrupted; check logs before citing numbers.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
