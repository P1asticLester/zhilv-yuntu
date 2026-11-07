"""契约测试：LLM_ENABLE_THINKING 开关必须原样传给两处 ChatOpenAI。

不调用真实模型、不需要网络与 API Key：把 langchain_openai 换成只记录构造参数的
假模块，断言 Planner 与 Query Rewrite 两处的 extra_body.enable_thinking 与配置同值。
"""

from pathlib import Path
import sys
from types import ModuleType

import pytest


# 允许测试文件直接导入 backend/app 下的模块。
CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.agents.tools.rag_tool as rag_tool  # noqa: E402
import app.agents.trip_planner_agent as trip_planner_agent  # noqa: E402


LLM_BUILDERS = (trip_planner_agent, rag_tool)


@pytest.fixture
def captured_llm_kwargs(monkeypatch):
    """用假模块拦住两处 _build_chat_llm()，只记录传给构造函数的参数。"""
    captured = []

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    fake_module = ModuleType("langchain_openai")
    fake_module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)

    # _build_chat_llm() 在没有 API Key 时会直接返回 None，这里给个假的非空值。
    for module in LLM_BUILDERS:
        monkeypatch.setattr(module, "LLM_API_KEY", "test-key")

    return captured


@pytest.mark.parametrize("enabled", [False, True])
def test_enable_thinking_switch_reaches_both_llm_builders(
    monkeypatch, captured_llm_kwargs, enabled
):
    """开关为 False / True 时，两处构造参数里的值必须完全相同。"""
    for module in LLM_BUILDERS:
        monkeypatch.setattr(module, "LLM_ENABLE_THINKING", enabled)

    for module in LLM_BUILDERS:
        assert module._build_chat_llm() is not None

    assert len(captured_llm_kwargs) == 2
    for kwargs in captured_llm_kwargs:
        assert kwargs["model_kwargs"] == {
            "extra_body": {"enable_thinking": enabled}
        }
