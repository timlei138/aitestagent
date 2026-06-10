from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


def _truncate_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "")
    text = text.replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


def _messages_preview(messages: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    preview = []
    for item in (messages or [])[:limit]:
        role = str(item.get("role", ""))
        content = item.get("content", "")
        preview.append({"role": role, "content": _truncate_text(content, 400)})
    return preview


class LLMClient(ABC):
    """文本模型抽象接口。"""

    @abstractmethod
    def invoke(self, messages: list[dict[str, Any]]) -> str:
        raise NotImplementedError


class VLMClient(ABC):
    """视觉模型抽象接口。"""

    @abstractmethod
    def describe(self, prompt: str, image_base64: str, context: str = "") -> str:
        raise NotImplementedError


class OpenAITextClient(LLMClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float = 0.1,
    ):
        from langchain_openai import ChatOpenAI

        self._client = ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=api_key,
            base_url=base_url,
        )

    def invoke(self, messages: list[dict[str, Any]]) -> str:
        try:
            logger.info(
                "LLM request provider=openai messages=%s",
                _messages_preview(messages),
            )
            response = self._client.invoke(messages)
            content = str(response.content)
            logger.info("LLM response provider=openai content=%s", _truncate_text(content))
            return content
        except Exception as exc:
            logger.error("OpenAITextClient invoke failed: %s", exc)
            raise


class OpenAIVisionClient(VLMClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float = 0.1,
    ):
        from langchain_openai import ChatOpenAI

        self._client = ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=api_key,
            base_url=base_url,
        )

    def describe(self, prompt: str, image_base64: str, context: str = "") -> str:
        image_url = f"data:image/png;base64,{image_base64}"
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": context or "请分析该截图"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ]
        try:
            logger.info(
                "VLM request provider=openai prompt=%s context=%s image_len=%s",
                _truncate_text(prompt, 400),
                _truncate_text(context, 400),
                len(image_base64 or ""),
            )
            response = self._client.invoke(messages)
            content = str(response.content)
            logger.info("VLM response provider=openai content=%s", _truncate_text(content))
            return content
        except Exception as exc:
            logger.error("OpenAIVisionClient describe failed: %s", exc)
            raise


class ZhipuTextClient(LLMClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float = 0.1,
    ):
        from zhipuai import ZhipuAI

        self.model = model
        self.temperature = temperature
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = ZhipuAI(**kwargs)

    def invoke(self, messages: list[dict[str, Any]]) -> str:
        try:
            logger.info(
                "LLM request provider=zhipu messages=%s",
                _messages_preview(messages),
            )
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
            )
            content = str(resp.choices[0].message.content)
            logger.info("LLM response provider=zhipu content=%s", _truncate_text(content))
            return content
        except Exception as exc:
            logger.error("ZhipuTextClient invoke failed: %s", exc)
            raise


class ZhipuVisionClient(VLMClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float = 0.1,
    ):
        from zhipuai import ZhipuAI

        self.model = model
        self.temperature = temperature
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = ZhipuAI(**kwargs)

    def describe(self, prompt: str, image_base64: str, context: str = "") -> str:
        image_url = f"data:image/png;base64,{image_base64}"
        try:
            logger.info(
                "VLM request provider=zhipu prompt=%s context=%s image_len=%s",
                _truncate_text(prompt, 400),
                _truncate_text(context, 400),
                len(image_base64 or ""),
            )
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": context or "请分析该截图"},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    },
                ],
                temperature=self.temperature,
            )
            content = str(resp.choices[0].message.content)
            logger.info("VLM response provider=zhipu content=%s", _truncate_text(content))
            return content
        except Exception as exc:
            logger.error("ZhipuVisionClient describe failed: %s", exc)
            raise


def create_llm_client(
    provider: str,
    model: str,
    api_key: str | None,
    base_url: str | None = None,
    temperature: float = 0.1,
) -> LLMClient | None:
    if not api_key:
        return None
    name = (provider or "openai").lower()
    if name == "zhipu":
        return ZhipuTextClient(
            model=model, api_key=api_key, base_url=base_url, temperature=temperature
        )
    return OpenAITextClient(
        model=model, api_key=api_key, base_url=base_url, temperature=temperature
    )


def create_vlm_client(
    provider: str,
    model: str,
    api_key: str | None,
    base_url: str | None = None,
    temperature: float = 0.1,
) -> VLMClient | None:
    if not api_key:
        return None
    name = (provider or "openai").lower()
    if name == "zhipu":
        return ZhipuVisionClient(
            model=model, api_key=api_key, base_url=base_url, temperature=temperature
        )
    return OpenAIVisionClient(
        model=model, api_key=api_key, base_url=base_url, temperature=temperature
    )
