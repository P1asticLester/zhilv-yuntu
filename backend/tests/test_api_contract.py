"""4b · 端点契约测试（补齐 3 个空白端点 + 校验 / 降级分支）。

底稿来自你写的 docs/mastery/13_4b_starter.py。**写对的断言原样保留**：
health 示范、root 精确值、stats 顶层 5 键集合、total_tokens 等式、
len(items) == trip_count、TokenUsage 的 8 个字段名。
只修 5 条红灯与 3 个未暴露的坑：
  1. detail 整段粘了另一次「坏 JSON」的响应 → 改成断言错误结构
  2. `ietm` 拼写；`item["usage"]` 实际字段是 `token_usage`（schemas.py:209）
  3. `> 0` 在空库 / 全 Mock 下必挂 → `>= 0`（TokenUsage 默认 0）
  4. DELETE 硬编码 trip_id 且没先 save → 每个测试自己造唯一数据
  5. DELETE 失败分支是 404 不是 422（trip.py:199）；
     weather 降级是 502 不是 200（conftest 置空 AMAP_API_KEY）

设计原则：每个测试自己造数据，不依赖别的测试先跑、也不依赖本机 app.db 历史行。
断网无密钥可跑（4a 的 conftest 自动生效）。

怎么看（PowerShell）：
    cd "D:/zhilv-yuntu/backend"
    & "./.venv/Scripts/python.exe" -m pytest -q tests/test_api_contract.py
    & "./.venv/Scripts/python.exe" -m pytest -q
"""
from pathlib import Path
import sys
import uuid

from fastapi.testclient import TestClient


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api.main import app  # noqa: E402


client = TestClient(app)


def _save_one_trip() -> str:
    """造一条 trip_id 唯一的行程，供 stats / delete 使用，返回实际落库的 id。"""
    payload = {
        "destination": "大理",
        "start_date": "2026-04-10",
        "end_date": "2026-04-12",
        "travelers": 2,
        "budget": 3200,
        "preferences": ["自然风景", "拍照"],
        "pace": "轻松",
    }
    generated = client.post("/trip/generate", json=payload)
    assert generated.status_code == 200

    itinerary = generated.json()
    # save_itinerary 以 itinerary.trip_id 落库，所以唯一值要改在行程体里。
    itinerary["trip_id"] = f"contract_{uuid.uuid4().hex[:8]}"
    saved = client.post(
        "/trip/save",
        json={
            "trip_id": itinerary["trip_id"],
            "itinerary": itinerary,
            "user_id": "user_001",
        },
    )
    assert saved.status_code == 200
    return saved.json()["trip_id"]


def test_contract_health_returns_ok_shape() -> None:
    """最小契约 = 状态码 + 响应体的准确形状。"""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_contract_root_returns_running_message() -> None:
    """根路径：精确断言响应体，而不只是「有 message 这个键」。"""
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "Trip Planner Demo backend is running."}


def test_contract_generate_rejects_invalid_request() -> None:
    """travelers=0 触发 schemas.py:13 的 ge=1；只断言错误结构，不粘整段响应。"""
    payload = {
        "destination": "大理",
        "start_date": "2026-04-10",
        "end_date": "2026-04-12",
        "travelers": 0,
        "budget": 3200,
    }
    response = client.post("/trip/generate", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    first_error = detail[0]
    assert {"loc", "type", "msg"} <= set(first_error.keys())
    assert "travelers" in first_error["loc"]


def test_contract_stats_returns_token_stats() -> None:
    """自己造一条数据，保证 items 非空、结构断言真的被执行。"""
    saved_trip_id = _save_one_trip()
    response = client.get("/trip/stats")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, dict)
    assert set(data.keys()) == {
        "trip_count",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_tokens",
        "items",
    }

    for key in {
        "trip_count",
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_tokens",
    }:
        assert isinstance(data[key], int)
        assert data[key] >= 0

    assert data["total_tokens"] == data["total_prompt_tokens"] + data["total_completion_tokens"]

    items = data["items"]
    assert isinstance(items, list)
    assert len(items) == data["trip_count"]
    assert any(item["trip_id"] == saved_trip_id for item in items)

    token_keys = {
        "rewrite_prompt_tokens",
        "rewrite_completion_tokens",
        "embedding_prompt_tokens",
        "embedding_completion_tokens",
        "planner_prompt_tokens",
        "planner_completion_tokens",
        "rerank_prompt_tokens",
        "rerank_completion_tokens",
    }

    for item in items:
        assert isinstance(item["trip_id"], str)
        assert isinstance(item["destination"], str) and item["destination"]

        # schemas.py:209 的字段是 token_usage，不是 usage。
        usage = item["token_usage"]
        assert isinstance(usage, dict)
        assert set(usage.keys()) == token_keys
        # TokenUsage 8 个字段默认 0（schemas.py:129-136），全 Mock 环境就是 0。
        for value in usage.values():
            assert isinstance(value, int) and value >= 0


def test_contract_delete_trip_returns_ok_shape() -> None:
    """先 save 再 delete：200 + message / trip_id。"""
    trip_id = _save_one_trip()
    response = client.delete(f"/trip/{trip_id}")

    assert response.status_code == 200
    assert response.json() == {
        "message": "Trip itinerary deleted successfully.",
        "trip_id": trip_id,
    }


def test_contract_delete_trip_returns_404_when_missing() -> None:
    """资源不存在是 404（trip.py:199），不是 422；同一 id 删第二次必然失败。"""
    trip_id = _save_one_trip()
    assert client.delete(f"/trip/{trip_id}").status_code == 200

    response = client.delete(f"/trip/{trip_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Trip not found."}


def test_contract_weather_forecast_returns_degraded_502() -> None:
    """4a 的 conftest 置空 AMAP_API_KEY，天气接口按约定降级为 502。"""
    response = client.get("/weather/forecast", params={"city": "大理"})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail
