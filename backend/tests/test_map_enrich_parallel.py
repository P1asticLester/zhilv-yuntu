"""② 地图 enrich 并行化 · 离线契约测试。

不联网、不调高德：把三个 _enrich_* 换成只记录行为的假函数，只验并行这一层的契约，
不重复验 _enrich_* 自己怎么请求高德（那部分有 test_place_candidate_service.py）。

守的四件事：
1. workers=1 时行为与改造前完全一致（顺序 / city / 字段 / note，且不建线程池）；
2. 并行时不漏任何一个景点 / 酒店 / 交通，且确实跑在多个线程上；
3. 单项失败（如 429）被隔离，其余照常返回，note 不重复；
4. 「env 字符串 → 并发度」的解析契约，非法值不会让进程崩或打爆上游 QPS。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.services.map_service as map_service  # noqa: E402
from app.models.schemas import (  # noqa: E402
    BudgetBreakdown,
    DayPlan,
    HotelItem,
    Itinerary,
    SpotItem,
    TransportItem,
)


MAP_NOTE = "已补充高德地图地址、坐标或路线估算信息。"
CONFIG_PATH = BACKEND_DIR / "app" / "config.py"


def _build_itinerary() -> Itinerary:
    """2 天 / 4 景点 / 2 酒店 / 2 段交通 = 8 个互不依赖的 enrich 任务。"""
    return Itinerary(
        trip_id="test-parallel",
        destination="大理",
        summary="测试行程",
        budget_breakdown=BudgetBreakdown(),
        days=[
            DayPlan(
                day_index=1,
                spots=[SpotItem(name="景点1"), SpotItem(name="景点2")],
                hotel=HotelItem(name="酒店1"),
                transport=[
                    TransportItem(mode="打车", from_place="古城", to_place="洱海")
                ],
            ),
            DayPlan(
                day_index=2,
                spots=[SpotItem(name="景点3"), SpotItem(name="景点4")],
                hotel=HotelItem(name="酒店2"),
                transport=[
                    TransportItem(mode="步行", from_place="酒店", to_place="苍山")
                ],
            ),
        ],
    )


def _filled_values(itinerary: Itinerary) -> list[str | None]:
    """按原来的嵌套 for 顺序取出每个对象上「补全成功」的标记字段。"""
    values: list[str | None] = []
    for day in itinerary.days:
        for spot in day.spots:
            values.append(spot.address)
        if day.hotel is not None:
            values.append(day.hotel.address)
        for transport in day.transport:
            values.append(transport.duration)
    return values


@pytest.fixture
def fake_enrichers(monkeypatch):
    """替换三个补全函数：记录调用、给对象打标记、模拟等待网络回包。"""
    calls: list[tuple[str, int, str | None, int]] = []
    lock = threading.Lock()

    def record(kind: str, item, city) -> None:
        with lock:
            calls.append((kind, id(item), city, threading.get_ident()))
        time.sleep(0.03)  # 放大「等网络回包」，让并发是否真的发生可被观测

    def fake_spot(spot, city=None) -> bool:
        record("spot", spot, city)
        spot.address = "已补全"
        return True

    def fake_hotel(hotel, city=None) -> bool:
        record("hotel", hotel, city)
        hotel.address = "已补全"
        return True

    def fake_transport(transport, city=None) -> bool:
        record("transport", transport, city)
        transport.duration = "已补全"
        return True

    monkeypatch.setattr(map_service, "_enrich_spot", fake_spot)
    monkeypatch.setattr(map_service, "_enrich_hotel", fake_hotel)
    monkeypatch.setattr(map_service, "_enrich_transport", fake_transport)
    return calls


# --------------------------------------------------------------- 1. 串行可回退
def test_workers_one_keeps_original_serial_behaviour(monkeypatch, fake_enrichers):
    """设 1 = 回到改造前：顺序、city、字段、note 全部一致，且不创建线程池。"""
    monkeypatch.setattr(map_service, "MAP_ENRICH_MAX_WORKERS", 1)

    def forbidden_pool(*_args, **_kwargs):
        raise AssertionError("workers=1 时不应创建线程池")

    monkeypatch.setattr(map_service, "ThreadPoolExecutor", forbidden_pool)

    itinerary = _build_itinerary()
    result = map_service.enrich_itinerary_with_map_data(itinerary)

    assert result is itinerary
    assert [call[0] for call in fake_enrichers] == [
        "spot", "spot", "hotel", "transport",
        "spot", "spot", "hotel", "transport",
    ]
    assert {call[2] for call in fake_enrichers} == {"大理"}
    assert len({call[3] for call in fake_enrichers}) == 1  # 全在主线程
    assert _filled_values(itinerary) == ["已补全"] * 8
    assert itinerary.source_notes == [MAP_NOTE]


# --------------------------------------------------------------- 2. 并行不漏数据
def test_workers_four_enriches_every_item_in_parallel(monkeypatch, fake_enrichers):
    """并发后 8 项一项不少；显式 city 优先；确实跑在多个线程上。"""
    monkeypatch.setattr(map_service, "MAP_ENRICH_MAX_WORKERS", 4)

    itinerary = _build_itinerary()
    map_service.enrich_itinerary_with_map_data(itinerary, city="丽江")

    assert len(fake_enrichers) == 8
    assert {call[0] for call in fake_enrichers} == {"spot", "hotel", "transport"}
    assert {call[2] for call in fake_enrichers} == {"丽江"}
    assert len({call[3] for call in fake_enrichers}) > 1
    assert _filled_values(itinerary) == ["已补全"] * 8
    assert itinerary.source_notes == [MAP_NOTE]


# --------------------------------------------------------------- 3. 失败隔离
def test_single_failure_is_isolated(monkeypatch, fake_enrichers):
    """模拟某一项 429：只有它没补上，其余照常返回，整体不抛异常。"""
    monkeypatch.setattr(map_service, "MAP_ENRICH_MAX_WORKERS", 4)

    def failing_spot(spot, city=None) -> bool:
        if spot.name == "景点1":
            raise RuntimeError("模拟高德 429，异常文本不应进日志")
        spot.address = "已补全"
        return True

    monkeypatch.setattr(map_service, "_enrich_spot", failing_spot)

    itinerary = _build_itinerary()
    map_service.enrich_itinerary_with_map_data(itinerary)

    values = _filled_values(itinerary)
    assert values.count(None) == 1
    assert values.count("已补全") == 7
    assert itinerary.source_notes == [MAP_NOTE]


def test_all_tasks_failing_leaves_source_notes_clean(monkeypatch):
    """全部降级时不该谎称「已补充地图信息」。"""
    monkeypatch.setattr(map_service, "MAP_ENRICH_MAX_WORKERS", 4)
    monkeypatch.setattr(map_service, "_enrich_spot", lambda *_a, **_k: False)
    monkeypatch.setattr(map_service, "_enrich_hotel", lambda *_a, **_k: False)
    monkeypatch.setattr(map_service, "_enrich_transport", lambda *_a, **_k: False)

    itinerary = _build_itinerary()
    map_service.enrich_itinerary_with_map_data(itinerary)
    assert itinerary.source_notes == []


def test_note_is_not_duplicated(monkeypatch, fake_enrichers):
    """重复调用 / 已有 note 时不能往 source_notes 里堆同一句话。"""
    monkeypatch.setattr(map_service, "MAP_ENRICH_MAX_WORKERS", 4)

    itinerary = _build_itinerary()
    itinerary.source_notes.append(MAP_NOTE)
    map_service.enrich_itinerary_with_map_data(itinerary)
    assert itinerary.source_notes == [MAP_NOTE]


def test_empty_itinerary_is_a_noop(monkeypatch):
    """没有待补全对象时不建线程池、不加 note。"""
    monkeypatch.setattr(map_service, "MAP_ENRICH_MAX_WORKERS", 6)

    itinerary = _build_itinerary()
    itinerary.days = []
    map_service.enrich_itinerary_with_map_data(itinerary)
    assert itinerary.source_notes == []


# --------------------------------------------------------------- 4. env 解析契约
def _workers_with_env(monkeypatch, raw_value):
    """在隔离命名空间里重载 config.py，只取 MAP_ENRICH_MAX_WORKERS。

    不 importlib.reload(app.config)：reload 会改写全局配置常量污染其它测试。
    屏蔽本机 .env，CI 上不存在该文件也不影响断言。
    """
    if raw_value is None:
        monkeypatch.delenv("MAP_ENRICH_MAX_WORKERS", raising=False)
    else:
        monkeypatch.setenv("MAP_ENRICH_MAX_WORKERS", raw_value)

    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: False)

    spec = importlib.util.spec_from_file_location("_isolated_app_config_workers", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MAP_ENRICH_MAX_WORKERS


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, 6),
        ("1", 1),
        ("8", 8),
        ("0", 6),
        ("-1", 6),
        ("abc", 6),
        ("99", 6),
        ("", 6),
    ],
    ids=[
        "unset-default-6", "serial-1", "explicit-8", "zero-falls-back",
        "negative-falls-back", "garbage-falls-back", "too-large-falls-back",
        "empty-falls-back",
    ],
)
def test_map_enrich_workers_env_parsing(monkeypatch, raw_value, expected):
    """默认 6；1 可回退串行；非法 / 越界值一律回退，不打爆上游 QPS。"""
    assert _workers_with_env(monkeypatch, raw_value) == expected
