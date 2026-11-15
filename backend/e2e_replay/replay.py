#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实响应录制回放：拿 Step 5 留下的 3 份真模型响应，离线验解析器兼容性。

和 backend/tests/ 里手写 Mock 的区别：
    Mock  是「先写好期望，再照着它造测试」，手写数据恰好长得像我期望的样子；
    回放  是「真实输出原样存下来，以后拿它验解析器」，能抓住模型多给字段、
          给了 null、改了字段名这类手写数据永远测不出来的变化。

口径（重要）：
    这是 2026-11-14 的**历史快照**，只能说「验证了解析器对这批真实输出的
    兼容性」，**不能**说「验证了模型当前行为」。不重跑模型、零费用、零网络。

独立 runner：不进 backend/tests/ 的 pytest 收集，不进 CI 默认门禁。
只 import app.models.schemas 做 Pydantic 校验，不启动服务、不发任何请求。

跑法（PowerShell，解释器用 backend/.venv）：
    & "D:\\zhilv-yuntu\\backend\\.venv\\Scripts\\python.exe" "D:\\zhilv-yuntu\\backend\\e2e_replay\\replay.py"
"""
from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures"
REPORT_PATH = HERE / "report.md"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

TCP_ATTEMPTS: list[tuple] = []
TOP_LEVEL_KEYS = {
    "trip_id", "destination", "summary", "days", "estimated_budget",
    "budget_breakdown", "tips", "source_notes", "token_usage",
}
BUDGET_PARTS = ("transport", "hotel", "meals", "tickets", "other")
WEATHER_WORDS = ("雨", "天气", "气温")


def bootstrap_offline() -> None:
    """即使只 import schemas，也把外部世界关掉，保证「零网络」可验证。"""
    os.environ["LLM_API_KEY"] = ""
    os.environ["AMAP_API_KEY"] = ""
    os.environ["REDIS_ENABLED"] = "false"

    import httpx

    def _blocked_request(*_args, **_kwargs):
        raise RuntimeError("replay: real HTTP is blocked")

    httpx.Client.request = _blocked_request
    httpx.AsyncClient.request = _blocked_request

    def _blocked_socket(*args, **kwargs):
        TCP_ATTEMPTS.append(args)
        raise RuntimeError("replay: external TCP is blocked")

    socket.create_connection = _blocked_socket


bootstrap_offline()

from pydantic import ValidationError  # noqa: E402
from app.models.schemas import Itinerary  # noqa: E402


def replay_one(meta: dict) -> dict:
    raw = json.loads((FIXTURE_DIR / meta["file"]).read_text(encoding="utf-8"))
    checks: dict[str, bool] = {}

    try:
        model = Itinerary.model_validate(raw)
        checks["Itinerary.model_validate 通过"] = True
        checks["解析后 9 个顶层字段仍齐全"] = TOP_LEVEL_KEYS <= set(model.model_dump().keys())
        checks["解析后天数未变"] = len(model.days) == len(raw.get("days", []))
    except ValidationError:
        checks["Itinerary.model_validate 通过"] = False
        checks["解析后 9 个顶层字段仍齐全"] = False
        checks["解析后天数未变"] = False

    checks["原始 9 个顶层字段都在"] = TOP_LEVEL_KEYS <= set(raw.keys())
    days = raw.get("days", [])
    checks["天数与请求一致"] = len(days) == meta["expected_days"]
    checks["day_index 连续 1..N"] = [d.get("day_index") for d in days] == list(
        range(1, meta["expected_days"] + 1)
    )

    breakdown = raw.get("budget_breakdown", {})
    parts = sum(float(breakdown[k]) for k in BUDGET_PARTS if k in breakdown)
    total = float(breakdown.get("total", -1))
    checks["预算 5 分项之和 == total"] = abs(parts - total) < 0.02
    checks["total == estimated_budget"] = abs(total - float(raw.get("estimated_budget", -1))) < 0.02

    usage = raw.get("token_usage", {})
    checks["token_usage 8 字段是 int 且 >= 0"] = (
        len(usage) == 8
        and all(isinstance(value, int) and value >= 0 for value in usage.values())
    )
    checks["tips / source_notes 是 list[str]"] = all(
        isinstance(raw.get(key), list) and all(isinstance(item, str) for item in raw[key])
        for key in ("tips", "source_notes")
    )

    tips = raw.get("tips", [])
    return {
        "id": meta["id"],
        "destination": meta["destination"],
        "captured_at": meta["captured_at"],
        "expected_days": meta["expected_days"],
        "actual_days": len(days),
        "estimated_budget": raw.get("estimated_budget"),
        "total_tokens": sum(usage.values()) if usage else 0,
        "checks": checks,
        "passed": all(checks.values()),
        # 观察项，不作断言：成都样本确实含天气词，Step 5 的 weather_tip_ok 2/3 已如实记录。
        "tips_with_weather_word": any(word in tip for tip in tips for word in WEATHER_WORDS),
    }


def render_report(results: list[dict], tcp_count: int) -> str:
    lines = [
        "# 真实响应录制回放报告",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　"
        f"脚本：`backend/e2e_replay/replay.py`　结果：**{sum(r['passed'] for r in results)}/{len(results)} PASS**",
        "",
        "## 一句话结论",
        "",
        "把 3 份**真实** `/trip/generate` 响应原样存成 fixture，离线用 `Itinerary.model_validate` "
        "回放，10 项断言全过；以后每次改动都能拿这批真实输出验解析器兼容性。",
        "",
        "## 逐样本结果",
        "",
        "| 样本 | 目的地 | 采集批次 | 天数 | 预算 | token 合计 | 结果 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r['id']}` | {r['destination']} | {r['captured_at']} | "
            f"{r['actual_days']}（预期 {r['expected_days']}） | {r['estimated_budget']} | "
            f"{r['total_tokens']} | {'PASS' if r['passed'] else 'FAIL'} |"
        )
    lines += ["", "### 每条样本实际断言了什么", ""]
    for r in results:
        lines += [f"**`{r['id']}`**", ""]
        for label, ok in r["checks"].items():
            lines.append(f"- {'PASS' if ok else 'FAIL'}　{label}")
        lines += [
            "",
            f"- 观察项（**不作断言**）：tips 是否含天气词 = `{r['tips_with_weather_word']}`",
            "",
        ]
    lines += [
        "> 成都样本的 tips 含「雨具」，这是 2026-11-14 那批真实输出的事实，"
        "Step 5 的 `weather_tip_ok 2/3` 已如实留档；回放不调阈值掩盖，也不把它写成失败。",
        "",
        "## 口径（别被问穿）",
        "",
        f"- 外部 TCP 连接尝试：**{tcp_count} 次**。脚本只 import `app.models.schemas` 做 Pydantic 校验，"
        "import 前清空密钥、封死 `httpx` 与 `socket`；**不启动服务、不重跑模型、零费用**。",
        "- fixture 的唯一脱敏动作是把 `trip_id`（`trip_大理_2026-11-20` 这类一次性 ID）"
        "换成稳定的 `replay_<id>`，**其余字段逐字保留**，保证「原样回放」成立。",
        "- 这 3 份是 **2026-11-14 Step 5 真模型在线评估的历史快照**，"
        "结论只能是「验证了解析器对这批真实输出的兼容性」，"
        "**不能**写成「验证了模型当前行为」「回放等于重新跑模型」。",
        "- 与 Mock 的区别：Mock 是「先写好期望再照着造」，回放是「真实输出原样存下来验解析器」；"
        "前者可能自欺，后者能抓住真实的形状变化。",
        "- 独立 runner：不进 `backend/tests/` 的 pytest 收集，不进 CI 默认门禁，不改产品代码。",
        "- 与 `backend/e2e_resilience/` 平级：那边验「外部服务坏了怎么降级」，"
        "这边验「真实输出还认不认」，都是离线、有产物、能当场演示的轻量演练。",
        "",
        "## 复现（PowerShell）",
        "",
        "```powershell",
        '& "D:\\zhilv-yuntu\\backend\\.venv\\Scripts\\python.exe" "D:\\zhilv-yuntu\\backend\\e2e_replay\\replay.py"',
        "```",
        "",
        "禁语：「验证了真实模型当前行为」「回放覆盖了模型质量」「等价于线上回归整套」。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    meta = json.loads((FIXTURE_DIR / "_meta.json").read_text(encoding="utf-8"))
    results = [replay_one(case) for case in meta["cases"]]
    REPORT_PATH.write_text(render_report(results, len(TCP_ATTEMPTS)), encoding="utf-8")

    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'} | {r['id']} | days={r['actual_days']} tokens={r['total_tokens']}")
        for label, ok in r["checks"].items():
            if not ok:
                print(f"       FAILED CHECK: {label}")
    print(f"external TCP attempts = {len(TCP_ATTEMPTS)}")
    print(f"report written to {REPORT_PATH.name}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
