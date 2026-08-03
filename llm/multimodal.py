from __future__ import annotations

import json
import logging
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# 按 (model, base_url) 隔离的视觉能力状态，避免主模型和视觉模型的探测结果串扰
_CAP_STATES: dict[tuple[str, str], dict] = {}
_CAP_LOCK = Lock()
_CLIENT_CACHE_LOCK = Lock()
_OPENAI_CLIENTS: dict[tuple[str, str, str, int], Any] = {}
_FAIL_STREAK_UNSUPPORTED_THRESHOLD = 2


def _get_cap(model: str, base_url: str | None) -> dict:
    """获取指定模型的视觉能力状态（线程安全）。"""
    key = (model, base_url or "")
    with _CAP_LOCK:
        if key not in _CAP_STATES:
            _CAP_STATES[key] = {"state": "unknown", "error": "", "fail_streak": 0}
        return _CAP_STATES[key]


def reset_vision_capability_state() -> None:
    with _CAP_LOCK:
        _CAP_STATES.clear()


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


def _record_probe_failure(message: str, cap: dict) -> tuple[int, bool]:
    with _CAP_LOCK:
        if _is_payload_format_error(message):
            cap["fail_streak"] += 1
        else:
            cap["fail_streak"] = 0
        streak = cap["fail_streak"]
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
                max_retries=0,  # 视觉调用不需要 SDK 层重试，超时即失败
            )
            _OPENAI_CLIENTS[cache_key] = client
    # 根据 base64 前缀自动检测图片格式（JPEG 以 /9j/ 开头，PNG 以 iVBOR 开头）
    mime = "image/jpeg" if image_base64.startswith("/9j/") else "image/png"
    msg = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{image_base64}"},
                },
            ],
        }
    ]
    img_kb = len(image_base64) * 3 // 4 // 1024
    logger.info(
        "[vision-invoke] calling model=%s base_url=%s timeout=%ds image=%dKB prompt_len=%d",
        model, base_url, timeout_sec, img_kb, len(prompt),
    )
    import time as _time
    t0 = _time.monotonic()
    try:
        resp = client.invoke(msg)
        elapsed = _time.monotonic() - t0
        content = str(getattr(resp, "content", "") or "")
        logger.info(
            "[vision-invoke] OK model=%s elapsed=%.1fs response_len=%d",
            model, elapsed, len(content),
        )
        return content
    except Exception as exc:
        elapsed = _time.monotonic() - t0
        logger.warning(
            "[vision-invoke] FAIL model=%s base_url=%s elapsed=%.1fs error_type=%s error=%s",
            model, base_url, elapsed, type(exc).__name__, exc,
        )
        raise


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
    timeout_sec: int = 30,
) -> dict[str, Any]:
    cap = _get_cap(model or "", base_url)
    cap_state = cap["state"]
    cap_error = cap["error"]

    logger.info(
        "[vision-call] purpose=%s provider=%s model=%s base_url=%s state=%s "
        "image_size=%d enabled=%s timeout_sec=%d",
        purpose, provider, model, base_url, cap_state,
        len(image_base64 or ""), vision_enabled, timeout_sec,
    )

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
        disable_msg = (
            "vision disabled by config (vision_enabled=false)"
            "，检测弹窗请改用 detect_popup()（基于 UI 树，不依赖 vision）"
        )
        with _CAP_LOCK:
            cap["state"] = "unsupported"
            cap["error"] = disable_msg
            cap["fail_streak"] = 0
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
                cap["state"] = "supported"
                cap["error"] = ""
                cap["fail_streak"] = 0
                cap_state = cap["state"]
        except Exception as exc:
            msg = str(exc)
            if _is_unsupported_error(msg, provider, model):
                with _CAP_LOCK:
                    cap["state"] = "unsupported"
                    cap["error"] = msg
                    cap["fail_streak"] = 0
                    cap_state = cap["state"]
                logger.warning("Multimodal unsupported: %s", msg)
                return _mk_result(
                    False,
                    "unsupported",
                    reason="model does not support vision",
                    error=msg,
                )

            streak, should_mark_unsupported = _record_probe_failure(msg, cap)
            if should_mark_unsupported:
                with _CAP_LOCK:
                    cap["state"] = "unsupported"
                    cap["error"] = msg
                    cap["fail_streak"] = 0
                    cap_state = cap["state"]
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
                cap["state"] = "unknown"
                cap["error"] = msg
                cap_state = cap["state"]
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
                cap["state"] = "unsupported"
                cap["error"] = msg
                cap["fail_streak"] = 0
            return _mk_result(
                False, "unsupported", reason="model does not support vision", error=msg
            )

        streak, should_mark_unsupported = _record_probe_failure(msg, cap)
        if should_mark_unsupported:
            with _CAP_LOCK:
                cap["state"] = "unsupported"
                cap["error"] = msg
                cap["fail_streak"] = 0
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
