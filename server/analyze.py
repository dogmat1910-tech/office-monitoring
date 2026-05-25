"""
LLM-анализ транскриптов встреч через OpenRouter.

OpenRouter — единая точка к Claude/GPT/Gemini. У пользователя уже есть аккаунт
(используется в qc-zvonki). Конфигурация через env:
- OM_OPENROUTER_API_KEY — обязательно
- OM_LLM_MODEL — по умолчанию anthropic/claude-sonnet-4-6
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

from prompts import SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger("worker")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.environ.get("OM_OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OM_LLM_MODEL", "anthropic/claude-sonnet-4-6")


def analyze_transcript(transcript: str) -> dict:
    """Возвращает dict с разбором: checklist, critical_errors, summary, ...
    Бросает исключение если API недоступен или ответ не JSON."""
    if not API_KEY:
        raise RuntimeError("OM_OPENROUTER_API_KEY не сконфигурирован")
    if not transcript.strip():
        raise ValueError("пустой транскрипт")

    user_prompt = build_user_prompt(transcript)
    log.info("analyze: model=%s transcript_len=%d", MODEL, len(transcript))
    t0 = time.monotonic()

    r = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "HTTP-Referer": "https://office.lkdzrkk.pro",
            "X-Title": "office-monitoring",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 2500,
            "response_format": {"type": "json_object"},
        },
        timeout=120.0,
    )
    r.raise_for_status()
    data = r.json()

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    elapsed = time.monotonic() - t0

    log.info("analyze: %.1f s, prompt_tokens=%s, completion_tokens=%s",
             elapsed, usage.get("prompt_tokens"), usage.get("completion_tokens"))

    parsed = json.loads(content)
    parsed["_meta"] = {
        "model": MODEL,
        "processing_time_seconds": elapsed,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }
    return parsed
