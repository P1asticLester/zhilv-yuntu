#!/usr/bin/env python3
"""POST /trip/generate 端到端计时（仓库内基准工具，位于 backend/scripts/）。

用法（PowerShell，解释器用 backend/.venv）：
    & "D:/zhilv-yuntu/backend/.venv/Scripts/python.exe" "D:/zhilv-yuntu/backend/scripts/bench_generate.py" --help
    & "...python.exe" ".../bench_generate.py" 3       # 位置参数，兼容原 docs/tools 用法
    & "...python.exe" ".../bench_generate.py" --runs 5
    & "...python.exe" ".../bench_generate.py" --out "D:/zhilv-yuntu/docs/tools/timing_result.csv"

环境变量：
    BENCH_BASE_URL     后端地址，默认 http://127.0.0.1:8000（沿用旧名，未改）
    BENCH_OUTPUT_DIR   产物目录，默认 <本脚本目录>/_local（已被 .gitignore 忽略）

前置：后端已启动（uvicorn 或 docker compose up -d），且 backend/.env 里的
      LLM_API_KEY 可用。脚本先探测 /health，不在线会直接提示并退出。

计时口径：
- 从发出请求到读完完整响应体，覆盖 Query Rewrite → Embedding → Chroma 召回
  → Rerank → Planner → 地图 enrich → 预算回算全过程。
- 当前 REDIS_ENABLED=false，每次都是完整冷链路，样本之间可比；
  若日后开启 Redis，请自行把「首次」与「缓存命中」分组统计。
- 每次生成会调用 2 次 LLM + 若干次高德接口，别一次跑太多轮。
- 跨批次（VPN / 代理 / 模型账户状态）数字不可相减，别写成「快了 N%」。

产物与快照边界：
- 默认写 backend/scripts/_local/timing_result.csv，**不进仓库**。
- backend/data/metrics/timing_result.csv 是 6a 看板读的**进仓静态快照**。
  本脚本默认绝不覆盖它；确需刷新时先备份，再显式 --out 指过去，
  保持 utf-8-sig(BOM) 与逐字节口径，并在 CHANGELOG 标注样本数与环境。
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BENCH_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = (
    os.environ.get("BENCH_OUTPUT_DIR", "").strip()
    or os.path.join(SCRIPTS_DIR, "_local")
)

REQUEST = {
    "destination": "大理",
    "start_date": "2026-11-10",
    "end_date": "2026-11-12",
    "travelers": 2,
    "budget": 3200,
    "preferences": ["自然风景", "拍照", "美食"],
    "pace": "轻松",
    "dietary_preferences": ["少辣"],
    "hotel_level": "舒适型",
    "special_notes": "不想太早起床，希望安排一个适合看日落的地点",
}

# TokenUsage 的 total_* 是 Pydantic 计算属性，不会出现在响应 JSON 里，需按分项求和
PROMPT_FIELDS = [
    "rewrite_prompt_tokens", "embedding_prompt_tokens",
    "planner_prompt_tokens", "rerank_prompt_tokens",
]
COMPLETION_FIELDS = [
    "rewrite_completion_tokens", "embedding_completion_tokens",
    "planner_completion_tokens", "rerank_completion_tokens",
]
FIELDS = ["run", "status", "seconds", "trip_id", "days",
          "prompt_tokens", "completion_tokens", "planner_out", "error"]


def health_ok():
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def run_once(i):
    payload = json.dumps(REQUEST, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/trip/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    row = dict.fromkeys(FIELDS, "")
    row.update({"run": i, "error": ""})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            body = r.read()
            row["status"] = r.status
    except urllib.error.HTTPError as e:
        row["status"] = e.code
        row["seconds"] = round(time.perf_counter() - t0, 2)
        row["error"] = e.read().decode("utf-8", errors="replace")[:200]
        return row
    except Exception as e:
        row["status"] = "ERR"
        row["seconds"] = round(time.perf_counter() - t0, 2)
        row["error"] = str(e)[:200]
        return row
    row["seconds"] = round(time.perf_counter() - t0, 2)

    try:
        data = json.loads(body.decode("utf-8"))
        row["trip_id"] = data.get("trip_id", "")
        row["days"] = len(data.get("days") or [])
        usage = data.get("token_usage") or {}
        row["prompt_tokens"] = sum(int(usage.get(k) or 0) for k in PROMPT_FIELDS)
        row["completion_tokens"] = sum(int(usage.get(k) or 0) for k in COMPLETION_FIELDS)
        row["planner_out"] = int(usage.get("planner_completion_tokens") or 0)
    except Exception as e:
        row["error"] = "响应解析失败: %s" % e
    return row


def main():
    parser = argparse.ArgumentParser(
        description="POST /trip/generate 端到端计时（需后端在线 + 真实 API Key）。"
    )
    parser.add_argument("runs", nargs="?", type=int, default=3,
                        help="计时次数（默认 3；兼容原 docs/tools 的位置参数写法）")
    parser.add_argument("--runs", dest="runs_opt", type=int, default=None,
                        help="计时次数，与位置参数等价，二选一")
    parser.add_argument("--out", default=None,
                        help="明细 CSV 路径；默认 $BENCH_OUTPUT_DIR/timing_result.csv "
                             "或 backend/scripts/_local/timing_result.csv")
    args = parser.parse_args()
    n = args.runs_opt if args.runs_opt is not None else args.runs
    if n < 1:
        print("ERROR: runs must be >= 1")
        return 2
    out = args.out or os.path.join(DEFAULT_OUT_DIR, "timing_result.csv")

    if not health_ok():
        print("后端未就绪：%s/health 不可达。" % BASE)
        print("请先启动：cd backend && uvicorn app.api.main:app --host 0.0.0.0 --port 8000")
        print("或：docker compose up -d")
        return 1

    print("目标 %s/trip/generate | 共 %d 次 | 大理 3 天 / 2 人 / 3200 元" % (BASE, n))
    print("计时含完整链路（检索 → 生成 → 地图 → 预算），当前未启用缓存，均为冷链路\n")
    rows = []
    for i in range(1, n + 1):
        r = run_once(i)
        rows.append(r)
        print("  第 %d 次: HTTP %-4s | %6.2fs | days=%-2s | prompt=%-6s completion=%-5s planner_out=%-5s %s"
              % (i, r["status"], r["seconds"], r["days"], r["prompt_tokens"],
                 r["completion_tokens"], r["planner_out"], r["error"]))

    ok = [r["seconds"] for r in rows
          if r["status"] == 200 and not r["error"] and isinstance(r["seconds"], float)]
    if ok:
        print("\n成功 %d/%d 次 | 中位数 %.2fs | 最快 %.2fs | 最慢 %.2fs | 平均 %.2fs"
              % (len(ok), n, statistics.median(ok), min(ok), max(ok), statistics.mean(ok)))
        print("planner 输出 token（正常应为几百，若上千说明思考模式没关掉）：%s"
              % ", ".join(str(r["planner_out"]) for r in rows if r["planner_out"] != ""))
    else:
        print("\n没有成功样本，请查看上面每行的错误信息与后端终端日志。")

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print("明细已写入 %s（默认目录被 .gitignore 忽略，不覆盖 backend/data/metrics 快照）" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
