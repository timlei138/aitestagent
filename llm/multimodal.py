from __future__ import annotations

import json
import logging
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

_CAP_STATE = "unknown"  # unknown | supported | unsupported
_CAP_ERROR = ""
_CAP_FAIL_STREAK = 0
_CAP_LOCK = Lock()
_CLIENT_CACHE_LOCK = Lock()
_OPENAI_CLIENTS: dict[tuple[str, str, str, int], Any] = {}
_FAIL_STREAK_UNSUPPORTED_THRESHOLD = 2


def reset_vision_capability_state() -> None:
    global _CAP_STATE, _CAP_ERROR, _CAP_FAIL_STREAK
    with _CAP_LOCK:
        _CAP_STATE = "unknown"
        _CAP_ERROR = ""
        _CAP_FAIL_STREAK = 0


def _mk_result(
    ok: bool,
    capability: str,
    decision: str = "unknown",
    reason: str = "",
    evidence: str = "",
    raw: str = "",
    error: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "capability": capability,
        "decision": decision,
        "reason": reason,
        "evidence": evidence,
        "raw": raw,
        "error": error,
        "data": data or {},
    }


def _is_payload_format_error(message: str) -> bool:
    msg = (message or "").lower()
    keys = (
        "unknown variant",
        "image_url",
        "expected 'text'",
        'expected "text"',
        "failed to deserialize the json body",
        "invalid_request_error",
        "400",
        "bad request",
    )
    return any(k in msg for k in keys)


def _is_unsupported_error(
    message: str,
    provider: str | None = None,
    model: str | None = None,
) -> bool:
    msg = (message or "").lower()
    name = (provider or "").lower()
    model_name = (model or "").lower()

    keys = (
        "does not support images",
        "image input is not supported",
        "multimodal is not supported",
        "unsupported image",
        "unsupported content type",
        "invalid image",
        "unknown variant",
        "expected 'text'",
        'expected "text"',
        "failed to deserialize the json body",
    )
    # Provider/model specific signatures for OpenAI-compatible backends (e.g. DeepSeek).
    if "deepseek" in model_name or "deepseek" in msg:
        keys = keys + ("image_url", "unknown variant 'image_url'")
    if name == "zhipu":
        keys = keys + ("unknown variant 'image_url'",)
    return any(k in msg for k in keys)


def _record_probe_failure(message: str) -> tuple[int, bool]:
    global _CAP_FAIL_STREAK
    with _CAP_LOCK:
        if _is_payload_format_error(message):
            _CAP_FAIL_STREAK += 1
        else:
            _CAP_FAIL_STREAK = 0
        streak = _CAP_FAIL_STREAK
    return streak, streak >= _FAIL_STREAK_UNSUPPORTED_THRESHOLD


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        try:
            data = json.loads(snippet)
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def _invoke_openai_multimodal(
    model: str,
    api_key: str,
    base_url: str | None,
    prompt: str,
    image_base64: str,
    timeout_sec: int,
) -> str:
    from langchain_openai import ChatOpenAI

    cache_key = (model, api_key, base_url or "", timeout_sec)
    with _CLIENT_CACHE_LOCK:
        client = _OPENAI_CLIENTS.get(cache_key)
        if client is None:
            client = ChatOpenAI(
                model=model,
                temperature=0.0,
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_sec,
            )
            _OPENAI_CLIENTS[cache_key] = client
    msg = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_base64}"},
                },
            ],
        }
    ]
    resp = client.invoke(msg)
    return str(getattr(resp, "content", "") or "")


def _invoke_multimodal(
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None,
    prompt: str,
    image_base64: str,
    timeout_sec: int,
) -> str:
    # 统一走 OpenAI 兼容多模态接口（zhipu 等通过 base_url 指向其 OpenAI 兼容端点）。
    return _invoke_openai_multimodal(
        model, api_key, base_url, prompt, image_base64, timeout_sec
    )


def multimodal_vision_call(
    prompt: str,
    image_base64: str,
    purpose: str,
    strict_json: bool = True,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    vision_enabled: bool = True,
    timeout_sec: int = 12,
) -> dict[str, Any]:
    global _CAP_STATE, _CAP_ERROR, _CAP_FAIL_STREAK

    with _CAP_LOCK:
        cap_state = _CAP_STATE
        cap_error = _CAP_ERROR

    if not image_base64:
        return _mk_result(False, cap_state, reason="empty image", error="empty image")
    if not api_key or not model:
        return _mk_result(
            False,
            cap_state,
            reason="missing llm credentials",
            error="missing llm credentials",
        )

    if not vision_enabled:
        disable_msg = "vision disabled by config (vision_enabled=false)"
        with _CAP_LOCK:
            _CAP_STATE = "unsupported"
            _CAP_ERROR = disable_msg
            _CAP_FAIL_STREAK = 0
        return _mk_result(
            False,
            "unsupported",
            reason="vision disabled by config",
            error=disable_msg,
        )

    # unsupported 时快速回退，避免反复调用
    if cap_state == "unsupported":
        return _mk_result(
            False,
            "unsupported",
            reason="vision capability unsupported",
            error=cap_error,
        )

    # 首次/未知状态：做最小探测
    if cap_state == "unknown":
        probe_prompt = '请只返回 JSON: {"decision":"yes","reason":"ok"}'
        try:
            probe_text = _invoke_multimodal(
                provider or "openai",
                model,
                api_key,
                base_url,
                probe_prompt,
                image_base64,
                timeout_sec,
            )
            if _extract_json(probe_text) is None:
                # 返回非 JSON 仍视为支持，仅降低后续解析期望
                logger.info(
                    "Multimodal probe returned non-JSON but accepted image input"
                )
            with _CAP_LOCK:
                _CAP_STATE = "supported"
                _CAP_ERROR = ""
                _CAP_FAIL_STREAK = 0
                cap_state = _CAP_STATE
        except Exception as exc:
            msg = str(exc)
            if _is_unsupported_error(msg, provider, model):
                with _CAP_LOCK:
                    _CAP_STATE = "unsupported"
                    _CAP_ERROR = msg
                    _CAP_FAIL_STREAK = 0
                    cap_state = _CAP_STATE
                logger.warning("Multimodal unsupported: %s", msg)
                return _mk_result(
                    False,
                    "unsupported",
                    reason="model does not support vision",
                    error=msg,
                )

            streak, should_mark_unsupported = _record_probe_failure(msg)
            if should_mark_unsupported:
                with _CAP_LOCK:
                    _CAP_STATE = "unsupported"
                    _CAP_ERROR = msg
                    _CAP_FAIL_STREAK = 0
                    cap_state = _CAP_STATE
                logger.warning(
                    "Multimodal marked unsupported by repeated probe failures: %s",
                    msg,
                )
                return _mk_result(
                    False,
                    "unsupported",
                    reason=(
                        "model likely does not support vision "
                        f"(repeated payload failures={streak})"
                    ),
                    error=msg,
                )

            # 网络/限流等临时问题，不标记 unsupported
            with _CAP_LOCK:
                _CAP_STATE = "unknown"
                _CAP_ERROR = msg
                cap_state = _CAP_STATE
            logger.warning("Multimodal probe transient failure: %s", msg)
            return _mk_result(False, "unknown", reason="probe failed", error=msg)

    query_prompt = prompt.strip()
    if strict_json:
        query_prompt += "\n请严格返回 JSON，且只返回 JSON。"
    try:
        text = _invoke_multimodal(
            provider or "openai",
            model,
            api_key,
            base_url,
            query_prompt,
            image_base64,
            timeout_sec,
        )
        data = _extract_json(text) if strict_json else None
        if strict_json and data:
            return _mk_result(
                True,
                cap_state,
                decision=str(data.get("decision", "unknown")),
                reason=str(data.get("reason", "")),
                evidence=str(data.get("evidence", "")),
                raw=text,
                data=data,
            )
        if strict_json:
            return _mk_result(
                True,
                cap_state,
                decision="unknown",
                reason="non-json response",
                raw=text,
            )
        return _mk_result(
            True, cap_state, decision="ok", reason="vision completed", raw=text
        )
    except Exception as exc:
        msg = str(exc)
        if _is_unsupported_error(msg, provider, model):
            with _CAP_LOCK:
                _CAP_STATE = "unsupported"
                _CAP_ERROR = msg
                _CAP_FAIL_STREAK = 0
            return _mk_result(
                False, "unsupported", reason="model does not support vision", error=msg
            )

        streak, should_mark_unsupported = _record_probe_failure(msg)
        if should_mark_unsupported:
            with _CAP_LOCK:
                _CAP_STATE = "unsupported"
                _CAP_ERROR = msg
                _CAP_FAIL_STREAK = 0
            return _mk_result(
                False,
                "unsupported",
                reason=(
                    "model likely does not support vision "
                    f"(repeated payload failures={streak})"
                ),
                error=msg,
            )

        return _mk_result(False, cap_state, reason="vision call failed", error=msg)
