"""契约测试：LLM_ENABLE_THINKING 开关必须原样传给两处 ChatOpenAI。

不调用真实模型、不需要网络与 API Key：把 langchain_openai 换成只记录构造参数的
假模块，断言 Planner 与 Query Rewrite 两处的 extra_body.enable_thinking 与配置同值。

另外验证「env 字符串 → 配置常量」这条解析契约（见文件末尾），
它守的是 config.py 里 os.getenv(...).lower() == "true" 的默认值与大小写处理。
"""

import importlib.util
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


# --------------------------------------------------------------- env 解析契约
CONFIG_PATH = BACKEND_DIR / "app" / "config.py"


def _config_value_with_env(monkeypatch, raw_value):
    """在隔离命名空间里重新加载 config.py，只取 LLM_ENABLE_THINKING。

    用独立模块而不是 importlib.reload(app.config)：reload 会改写全局配置常量，
    可能污染同一进程里其它测试；这里只验「env 字符串 → 布尔常量」这条契约。
    """
    if raw_value is None:
        monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    else:
        monkeypatch.setenv("LLM_ENABLE_THINKING", raw_value)

    # 屏蔽本机 backend/.env：CI 上不存在该文件，测试不能依赖它是否存在或写了什么。
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: False)

    spec = importlib.util.spec_from_file_location("_isolated_app_config", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LLM_ENABLE_THINKING


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(None, False), ("true", True), ("false", False)],
    ids=["unset-default-false", "true", "false"],
)
def test_llm_enable_thinking_env_parsing(monkeypatch, raw_value, expected):
    """不设 env 默认关；显式 true 才开；显式 false 保持关。

    这组断言守的是配置解析本身：把 .lower() == "true" 改回写死 False，
    或把默认值改成 true，都会在这里失败，而不是等到线上才发现。
    """
    assert _config_value_with_env(monkeypatch, raw_value) is expected
