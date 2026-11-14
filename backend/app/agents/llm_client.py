"""LLM 客户端构造与 token usage 读取的唯一出处。

Planner 与 Query Rewrite 两个调用点只负责传入各自的 temperature 和从
自己模块读到的配置值；构造细节与 usage 结构在这里统一，避免改一处漏一处。
"""

from __future__ import annotations

def empty_token_usage() -> dict[str, int]:
    """每次返回新字典，避免调用方就地修改污染其它链路。"""
    return {"prompt_tokens": 0, "completion_tokens": 0}


def build_chat_llm(
    *,
    model: str,
    temperature: float,
    api_key: str,
    base_url: str,
    timeout: int,
    max_retries: int,
    enable_thinking: bool,
):
    """创建 ChatOpenAI；没有 key 或依赖缺失时返回 None，由调用方降级。"""
    if not api_key:
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return None

    # qwen3.7-plus 等混合思考模型默认开启 thinking，会先产生大量 reasoning tokens。
    # 本项目只需要结构化 JSON，显式固定该行为，不依赖上游未承诺的默认值。
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=api_key,
        base_url=base_url or None,
        timeout=timeout,
        max_retries=max_retries,
        model_kwargs={"extra_body": {"enable_thinking": enable_thinking}},
    )


def extract_token_usage(response) -> dict[str, int]:
    """从 LangChain AIMessage 的 response_metadata 里提取 token 用量。"""
    usage = empty_token_usage()
    metadata = getattr(response, "response_metadata", None) or {}
    token_usage = metadata.get("token_usage", {})
    if token_usage:
        usage["prompt_tokens"] = token_usage.get("prompt_tokens", 0)
        usage["completion_tokens"] = token_usage.get("completion_tokens", 0)
    return usage
