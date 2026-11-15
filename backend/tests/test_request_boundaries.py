"""接口层边界值回归：与 frontend/e2e/CASES.md ⑦ 同一组输入，打真实 Pydantic。

UI 层那 5 组用 page.route() 把后端响应固定成 422，只验「请求体原样透传 +
错误态呈现」，**没有回答「这些值该不该被拒绝」**。本文件用 TestClient 打到
真实的 TripRequest（schemas.py:7-22），把结论补上，形成「一张设计表两层落地」。

2026-11-15 先实跑再写断言，conftest 全 Mock / 断网无密钥下的真实行为：

| 输入 | 状态码 | 原因 |
| --- | --- | --- |
| 有效基线（3 天） | 200 | days=3 |
| 日期倒挂 | 200 | 无跨字段日期校验，服务层按 max(..., 1) 兜底 |
| travelers=0 / -1 | 422 | Field(ge=1)，detail 是 Pydantic list |
| budget=-1 | 422 | Field(ge=0)，detail 是 Pydantic list |
| budget=999999999 | 200 | 无上界，合法 |
| 空目的地 | 422 | 路由层自定义 dict（code=invalid_destination），不是 detail list |
| 省级「青海」 | 503 | 城市解析需高德 key，Mock 环境降级 city_resolution_unavailable |

因此 UI 打桩里「5 组统一 422」「青海 → 422 请输入具体城市」与真实契约有偏差，
已在 CASES.md 记明。本文件不改产品代码、不放宽任何断言；日期倒挂与极大预算
当前被接受属**已知校验缺口**，写成断言是为了让以后补校验时立刻红灯，而不是
假装它们已被拒绝。

怎么看（PowerShell）：
    cd "D:/zhilv-yuntu/backend"
    & "./.venv/Scripts/python.exe" -m pytest -q tests/test_request_boundaries.py
"""
import pytest
from fastapi.testclient import TestClient

from app.api.main import app


client = TestClient(app)

BASE = {
    "destination": "大理",
    "start_date": "2026-12-01",
    "end_date": "2026-12-03",
    "travelers": 2,
    "budget": 3200,
}


@pytest.mark.parametrize(
    ("patch", "expected_status", "expected_field"),
    [
        pytest.param(
            {"start_date": "2026-12-10", "end_date": "2026-12-01"},
            200,
            None,
            id="date-reversed-currently-accepted",
        ),
        pytest.param({"travelers": 0}, 422, "travelers", id="travelers-zero-rejected"),
        pytest.param({"travelers": -1}, 422, "travelers", id="travelers-negative-rejected"),
        pytest.param({"budget": -1}, 422, "budget", id="budget-negative-rejected"),
        pytest.param(
            {"budget": 999999999},
            200,
            None,
            id="budget-huge-currently-accepted",
        ),
    ],
)
def test_boundary_values_against_real_schema(patch, expected_status, expected_field) -> None:
    """与 CASES.md ⑦ 完全相同的 5 组；期望值来自实跑，不是想象。"""
    response = client.post("/trip/generate", json={**BASE, **patch})

    assert response.status_code == expected_status
    body = response.json()

    if expected_status == 422:
        detail = body["detail"]
        assert isinstance(detail, list) and detail
        first_error = detail[0]
        assert {"loc", "type", "msg"} <= set(first_error.keys())
        assert first_error["type"] == "greater_than_equal"
        assert expected_field in first_error["loc"]
    else:
        # 当前实现不拒绝：至少要把行程正常生成出来，不能冒 500 / 空天。
        assert len(body.get("days", [])) >= 1


def test_valid_equivalence_class_returns_full_day_count() -> None:
    """有效等价类正向对照：3 天请求 → 3 天行程，证明上面的 200 不是「全都兜底」。"""
    response = client.post("/trip/generate", json=BASE)

    assert response.status_code == 200
    assert len(response.json()["days"]) == 3


def test_empty_destination_uses_route_level_error_dict() -> None:
    """空目的地 422 来自路由层，不是 Pydantic：错误体是 dict 而非 detail list。"""
    response = client.post("/trip/generate", json={**BASE, "destination": ""})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "invalid_destination"


def test_province_destination_degrades_to_503_without_map_key() -> None:
    """省级目的地走城市解析，无高德 key 时降级 503（与 UI 打桩的 422 不同）。"""
    response = client.post("/trip/generate", json={**BASE, "destination": "青海"})

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "city_resolution_unavailable"
