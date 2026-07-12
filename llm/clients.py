from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# ── 重试参数 ──
_MAX_RETRIES = 3
_RETRY_WAIT_SECONDS = 5


class LLMFatalError(Exception):
    """LLM 不可重试的致命错误（如欠费、认证失败），调用方应终止并通知用户。"""
    pass


def _is_rate_limit_error(exc: Exception) -> bool:
    """判断异常是否为限流错误（429 Too Many Requests）。"""
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status == 429:
        return True
    resp = getattr(exc, "response", None) or getattr(exc, "resp", None)
    if resp and getattr(resp, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate" in msg or "too many requests" in msg


def _default_should_retry(exc: Exception) -> bool:
    """默认重试策略：大部分错误可重试，但认证/欠费等致命错误不可重试。"""
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status in (401, 402, 403):
        return False
    resp = getattr(exc, "response", None) or getattr(exc, "resp", None)
    if resp and getattr(resp, "status_code", None) in (401, 402, 403):
        return False
    msg = str(exc).lower()
    fatal_keywords = [
        "insufficient", "quota", "balance", "invalid api key",
        "authentication", "unauthorized", "forbidden",
    ]
    if any(kw in msg for kw in fatal_keywords):
        return False
    return True


def _call_with_retry(should_retry_fn, fn, *args, **kwargs):
    """带重试的调用，根据 should_retry_fn 判断是否重试。

    Returns: fn 的返回值，或 None（所有重试耗尽后降级）
    Raises:  LLMFatalError: 不可重试的错误
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not should_retry_fn(exc):
                logger.error("LLM fatal error (not retryable): %s", exc)
                raise LLMFatalError(str(exc)) from exc
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "LLM call failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, _MAX_RETRIES, _RETRY_WAIT_SECONDS, exc,
                )
                time.sleep(_RETRY_WAIT_SECONDS)
                continue
            logger.error("LLM call failed after %d retries, degrading: %s", _MAX_RETRIES, exc)
            return None


def _truncate_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


def _msg_role(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("role", ""))
    role = getattr(item, "type", None) or getattr(item, "role", None) or ""
    if role in ("ai", "assistant"): return "assistant"
    if role == "human": return "user"
    if role == "system": return "system"
    return str(role)


def _msg_content(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("content", ""))
    return str(getattr(item, "content", "") or "")


def _messages_preview(messages: list[Any], limit: int = 3) -> list[dict[str, Any]]:
    preview = []
    for item in (messages or [])[:limit]:
        preview.append({"role": _msg_role(item), "content": _truncate_text(_msg_content(item), 400)})
    return preview


# ── 抽象接口 ──

class LLMClient(ABC):
    """文本模型抽象接口。"""

    @abstractmethod
    def invoke(self, messages: list[Any]) -> str:
        raise NotImplementedError

    def should_retry(self, exc: Exception) -> bool:
        """判断异常是否可重试。默认：大部分错误可重试，认证/欠费等不可重试。子类按需覆盖。"""
        return _default_should_retry(exc)

    def supports_structured_output(self) -> bool:
        """是否支持 structured output（response_format 参数）。
        默认 False，子类按需重写。
        """
        return False


class VLMClient(ABC):
    """视觉模型抽象接口。"""

    @abstractmethod
    def describe(self, prompt: str, image_base64: str, context: str = "") -> str:
        raise NotImplementedError

    def should_retry(self, exc: Exception) -> bool:
        """判断异常是否可重试。默认：大部分错误可重试，认证/欠费等不可重试。子类按需覆盖。"""
        return _default_should_retry(exc)


# ── OpenAI 实现 ──

class OpenAITextClient(LLMClient):
    # 已知不支持 response_format 的 OpenAI 兼容 provider
    _NO_STRUCTURED_OUTPUT_HOSTS = ("deepseek", "together", "groq", "openrouter")

    def __init__(self, model: str, api_key: str, base_url: str | None = None, temperature: float = 0.1):
        from langchain_openai import ChatOpenAI
        self._base_url = base_url or ""
        self._client = ChatOpenAI(model=model, temperature=temperature, api_key=api_key, base_url=base_url)

    def invoke(self, messages: list[Any]) -> str:
        logger.info("LLM request provider=openai messages=%s", _messages_preview(messages))
        response = _call_with_retry(self.should_retry, self._client.invoke, messages)
        if response is None:
            return ""
        content = str(response.content)
        logger.info("LLM response provider=openai content=%s", _truncate_text(content))
        return content

    def supports_structured_output(self) -> bool:
        """OpenAI 原生 API 支持，但 DeepSeek/Together/Groq 等兼容 provider 不支持。"""
        return not any(h in self._base_url for h in self._NO_STRUCTURED_OUTPUT_HOSTS)


class OpenAIVisionClient(VLMClient):
    def __init__(self, model: str, api_key: str, base_url: str | None = None, temperature: float = 0.1):
        from langchain_openai import ChatOpenAI
        self._client = ChatOpenAI(model=model, temperature=temperature, api_key=api_key, base_url=base_url)

    def describe(self, prompt: str, image_base64: str, context: str = "") -> str:
        image_url = f"data:image/png;base64,{image_base64}"
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": [
                {"type": "text", "text": context or "请分析该截图"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]},
        ]
        logger.info("VLM request provider=openai image_len=%s", len(image_base64 or ""))
        response = _call_with_retry(self.should_retry, self._client.invoke, messages)
        if response is None:
            return ""
        content = str(response.content)
        logger.info("VLM response provider=openai content=%s", _truncate_text(content))
        return content


# ── 能力检查（无需实例化）──

def supports_structured_output(provider: str, base_url: str | None = None) -> bool:
    """检查指定 OpenAI 兼容端点是否支持 structured output (response_format 参数)。

    统一 OpenAI 兼容接入后仅按 base_url 判定：deepseek/groq/together/openrouter
    等兼容 provider 不支持此功能（provider 名不再参与判断）。
    """
    url = (base_url or "").lower()
    return not any(h in url for h in ("deepseek", "together", "groq", "openrouter"))


# ── 工厂 ──

def create_llm_client(
    provider: str,
    model: str,
    api_key: str | None,
    base_url: str | None = None,
    temperature: float = 0.1,
) -> LLMClient | None:
    if not api_key:
        return None
    # 统一走 OpenAI 兼容接入：zhipu 等 provider 通过 base_url 指向其 OpenAI 兼容端点。
    return OpenAITextClient(model=model, api_key=api_key, base_url=base_url, temperature=temperature)


def create_vlm_client(
    provider: str,
    model: str,
    api_key: str | None,
    base_url: str | None = None,
    temperature: float = 0.1,
) -> VLMClient | None:
    if not api_key:
        return None
    # 统一走 OpenAI 兼容接入：zhipu / 多模态 provider 通过 base_url 指向其 OpenAI 兼容端点。
    return OpenAIVisionClient(model=model, api_key=api_key, base_url=base_url, temperature=temperature)
