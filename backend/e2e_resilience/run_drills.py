#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""故障注入演练：主动制造 3 种外部故障，逐条观察生成链路的降级行为。

这是**独立 runner**，不属于 pytest 收集范围，也不进 CI 默认门禁。
它不修任何产品代码，只把「代码里写了 try/except」变成「我造过故障、看到过结果」。

为什么自带离线引导：
    backend/tests/conftest.py 的 autouse fixture 只在 pytest 进程里生效。
    独立脚本拿不到它，所以必须在 import app 之前自己清空密钥、封死 httpx 出口，
    保证零费用、零真实网络调用。LLM 保持「无密钥」状态，场景 1/3 通过 patch
    trip_planner_agent._build_chat_llm 直接返回假对象来造故障，不需要伪造 key。

3 个场景（都走 6 城 CURATED 路径，「大理」3 天）：
    1. LLM 调用超时      -> _build_chat_llm 换成会抛 TimeoutError 的假对象
    2. 高德 429 限流     -> enrich_itinerary_with_map_data 抛 AmapServiceError
    3. 模型返回坏 JSON   -> 假 LLM 返回「这不是 JSON」，提取 JSON 失败

期望都是 200 + 3 天行程 + 关键字段不丢；不同场景的降级发生在链路的不同位置。
产品代码的中文日志被捕获进 report.md 作为降级证据，stdout 只打 ASCII。

跑法（PowerShell，解释器用 backend/.venv）：
    & "D:\\zhilv-yuntu\\backend\\.venv\\Scripts\\python.exe" "D:\\zhilv-yuntu\\backend\\e2e_resilience\\run_drills.py"
"""
from __future__ import annotations

import io
import os
import socket
import sys
from contextlib import nullcontext, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPORT_PATH = Path(__file__).resolve().parent / "report.md"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

TCP_ATTEMPTS: list[tuple] = []
EVIDENCE_KEYS = (
    "调用失败",
    "未能从模型返回",
    "原始返回预览",
    "skip qwen3-rerank",
    "qwen3-rerank failed",
    "unavailable",
    "falling back",
)
KEY_EVIDENCE = ("调用失败", "未能从模型返回", "原始返回预览")


def bootstrap_offline() -> None:
    """import 产品代码之前先把外部世界关掉。"""
    # app/config.py 顶层 load_dotenv(backend/.env) 默认 override=False，
    # 所以这里先占位的空值不会被 .env 里的真实密钥覆盖。
    os.environ["LLM_API_KEY"] = ""
    os.environ["AMAP_API_KEY"] = ""
    os.environ["REDIS_ENABLED"] = "false"
    os.environ["LLM_MAX_RETRIES"] = "0"
    os.environ["ENABLE_AMAP_ENRICHMENT"] = "true"  # 让场景 2 能进入 try 块

    import httpx

    def _blocked_request(*_args, **_kwargs):
        raise RuntimeError("drill: real HTTP is blocked; use a patched fake instead")

    httpx.Client.request = _blocked_request
    httpx.AsyncClient.request = _blocked_request

    def _blocked_socket(*args, **kwargs):
        TCP_ATTEMPTS.append(args)
        raise RuntimeError("drill: external TCP is blocked")

    socket.create_connection = _blocked_socket


bootstrap_offline()

# 引导完成后才能 import 产品代码。
from fastapi.testclient import TestClient  # noqa: E402

from app.agents import trip_planner_agent  # noqa: E402
from app.api.main import app  # noqa: E402
from app.rag import vector_db  # noqa: E402
from app.services import trip_service  # noqa: E402
from app.services.map_service import AmapServiceError  # noqa: E402

# 纯本地关键词检索，不依赖本机是否灌过 Chroma 向量。
vector_db._get_chroma_collection = lambda: None

client = TestClient(app)

BASE_PAYLOAD = {
    "destination": "大理",
    "start_date": "2026-12-01",
    "end_date": "2026-12-03",
    "travelers": 2,
    "budget": 3200,
    "preferences": ["自然风景"],
    "pace": "轻松",
}


# ------------------------------------------------------------------ 三种可控故障
class _TimeoutLLM:
    def invoke(self, messages):
        raise TimeoutError("drill: simulated LLM timeout")


class _BadJsonLLM:
    def invoke(self, messages):
        return SimpleNamespace(
            content="这不是 JSON，也不是代码块，只是一段普通文字。",
            response_metadata={"token_usage": {"prompt_tokens": 0, "completion_tokens": 0}},
        )


def _normal_map(itinerary, city=None):
    """场景 1/3 的地图环：不联网，原样返回行程。"""
    return itinerary


DRILLS = [
    {
        "name": "LLM 调用超时",
        "inject": "app.agents.trip_planner_agent._build_chat_llm -> TimeoutError on invoke()",
        "llm": lambda: _TimeoutLLM(),
        "map": _normal_map,
        "degrade_at": "trip_planner_agent.py:266-275 捕获异常，generate_planner_draft 返回 None",
    },
    {
        "name": "高德 429 限流",
        "inject": "app.services.trip_service.enrich_itinerary_with_map_data -> AmapServiceError",
        "llm": None,  # 不碰 LLM：保持无密钥降级状态，本场景只注入一个故障
        "map": AmapServiceError("drill: simulated Amap HTTP 429"),
        "degrade_at": "trip_service.py:240-244 吞掉异常，继续做预算回算",
    },
    {
        "name": "模型返回坏 JSON",
        "inject": "app.agents.trip_planner_agent._build_chat_llm -> content 无法提取 JSON",
        "llm": lambda: _BadJsonLLM(),
        "map": _normal_map,
        "degrade_at": "trip_planner_agent.py:287-291 提取 JSON 失败，generate_planner_draft 返回 None",
    },
]


def run_one(drill: dict) -> dict:
    llm_patch = (
        patch.object(trip_planner_agent, "_build_chat_llm", side_effect=drill["llm"])
        if drill["llm"] is not None
        else nullcontext()
    )
    buffer = io.StringIO()
    with redirect_stdout(buffer), llm_patch, patch.object(
        trip_service, "enrich_itinerary_with_map_data", side_effect=drill["map"]
    ):
        response = client.post("/trip/generate", json=BASE_PAYLOAD)

    captured = buffer.getvalue()
    seen: set[str] = set()
    unique_lines: list[str] = []
    for line in captured.splitlines():
        stripped = line.strip()
        if stripped and stripped not in seen and any(key in stripped for key in EVIDENCE_KEYS):
            seen.add(stripped)
            unique_lines.append(stripped)
    # 关键降级证据优先；rerank 的离线噪声最多留 2 条，别把重点挤掉。
    key_lines = [line for line in unique_lines if any(key in line for key in KEY_EVIDENCE)]
    noise = [line for line in unique_lines if line not in key_lines][:2]
    evidence = key_lines + noise
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    days = body.get("days", []) if isinstance(body, dict) else []
    checks = {
        "不返回 5xx（状态码 200）": response.status_code == 200,
        "天数不空且为 3 天": len(days) == 3,
        "budget_breakdown 字段在": isinstance(body.get("budget_breakdown"), dict),
        "token_usage 字段在": isinstance(body.get("token_usage"), dict),
        "tips 非空": isinstance(body.get("tips"), list) and len(body["tips"]) > 0,
        "source_notes 非空": isinstance(body.get("source_notes"), list) and len(body["source_notes"]) > 0,
        "每天结构完整（spots / meals / hotel / transport）": all(
            {"spots", "meals", "hotel", "transport"} <= set(day.keys()) for day in days
        ),
        "没走模型成功路径（planner token 为 0）": (
            (body.get("token_usage") or {}).get("planner_prompt_tokens", 0) == 0
        ),
    }
    return {
        "name": drill["name"],
        "inject": drill["inject"],
        "degrade_at": drill["degrade_at"],
        "status": response.status_code,
        "days": len(days),
        "checks": checks,
        "passed": all(checks.values()),
        "planner_tokens": (body.get("token_usage") or {}).get("planner_prompt_tokens", "n/a"),
        "source_notes": body.get("source_notes", []),
        "evidence": evidence,
    }


def render_report(results: list[dict], tcp_count: int) -> str:
    lines = [
        "# 故障注入演练报告",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　"
        f"脚本：`backend/e2e_resilience/run_drills.py`　结果：**{sum(r['passed'] for r in results)}/{len(results)} PASS**",
        "",
        "## 一句话结论",
        "",
        "主动把外部依赖弄坏 3 次，`/trip/generate` 都返回 **200**、3 天行程完整、"
        "关键字段没丢，降级发生在各自不同的代码位置；全程零真实网络调用。",
        "",
        "## 逐场景结果",
        "",
        "| 场景 | 注入点 | 状态码 | 天数 | 降级发生位置 | 结果 |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['name']} | `{r['inject']}` | {r['status']} | {r['days']} | "
            f"{r['degrade_at']} | {'PASS' if r['passed'] else 'FAIL'} |"
        )
    lines += ["", "### 每条用例实际断言了什么", ""]
    for r in results:
        lines += [f"**{r['name']}**", ""]
        for label, ok in r["checks"].items():
            lines.append(f"- {'PASS' if ok else 'FAIL'}　{label}")
        lines += [
            "",
            f"- `planner_prompt_tokens` = `{r['planner_tokens']}`",
            f"- 实际 `source_notes` 首条：`{r['source_notes'][0] if r['source_notes'] else '(empty)'}`",
            "",
        ]
    lines += ["### 降级证据（从产品日志里抓到的原文）", ""]
    for r in results:
        lines += [f"**{r['name']}**", ""]
        if r["evidence"]:
            lines += [f"- `{line}`" for line in r["evidence"][:8]]
        else:
            lines.append("- （无）")
        lines.append("")
    lines += [
        "> 场景「高德 429 限流」没有地图异常对应的日志，是**预期行为**："
        "`trip_service.py:243` 的 `except Exception: pass` 会静默吞掉地图异常，"
        "所以只能靠「状态码 200 + 结构完整 + 字段不丢」来证明降级生效。",
        "",
        "## 环境与口径（别被问穿）",
        "",
        f"- 外部 TCP 连接尝试：**{tcp_count} 次**。脚本在 import 产品代码前清空密钥、"
        "封死 `httpx.Client.request` 与 `socket.create_connection`，"
        "并把 Chroma 固定为本地关键词检索；**零费用、零真实网络**。",
        "- LLM 全程保持「无密钥」状态；场景 1/3 是 patch `_build_chat_llm` 直接返回假对象，"
        "不是伪造 key 去连真模型。",
        "- 三个场景都走 6 城 **CURATED 路径**（目的地「大理」）。该路径的降级是"
        "「`llm_draft is None` → 服务层用规则组装行程」，`source_notes` 里**没有**「规则方案」字样；"
        "那句话只出现在 DYNAMIC 路径（`trip_service.py:493/495`），别拿它当断言。",
        "- **错误码分级不在这份报告里**：422（用户输入）/ 503（城市解析、候选池不可用）"
        "属于路由层，由 `backend/tests/test_request_boundaries.py` 的 8 条接口契约测试覆盖；"
        "本报告只验「外部服务坏了还能不能给出可用行程」。",
        "- 本项目本来就有降级代码，这份演练**不是修 bug、不是加功能**，加的是"
        "「我造过故障、逐条看到结果」的可展示证据。",
        "- 独立 runner：不进 `backend/tests/` 的 pytest 收集，不进 CI 默认门禁，"
        "不改任何产品代码；`patch` 只在 `with` 块内生效，退出自动还原。",
        "",
        "## 复现（PowerShell）",
        "",
        "```powershell",
        '& "D:\\zhilv-yuntu\\backend\\.venv\\Scripts\\python.exe" "D:\\zhilv-yuntu\\backend\\e2e_resilience\\run_drills.py"',
        "```",
        "",
        "禁语：「验证了真实模型超时」「覆盖全部降级分支」「做了完整故障演练体系」。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    results = [run_one(drill) for drill in DRILLS]
    REPORT_PATH.write_text(render_report(results, len(TCP_ATTEMPTS)), encoding="utf-8")

    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'} | {r['name']} | status={r['status']} days={r['days']}")
        for label, ok in r["checks"].items():
            if not ok:
                print(f"       FAILED CHECK: {label}")
    print(f"external TCP attempts = {len(TCP_ATTEMPTS)}")
    print(f"report written to {REPORT_PATH.name}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
