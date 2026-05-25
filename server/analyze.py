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
import re
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

    parsed = _extract_json(content)
    parsed["_meta"] = {
        "model": MODEL,
        "processing_time_seconds": elapsed,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }
    return parsed


def _extract_json(content: str) -> dict:
    """Достаёт JSON из ответа LLM. Терпим к markdown-обёрткам и преамбулам."""
    s = content.strip()
    # 1) чистый JSON
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 2) ```json ... ``` или ``` ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3) первый сбалансированный {...}
    start = s.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    log.warning("LLM вернул не-JSON, первые 500 символов: %s", s[:500])
    raise ValueError(f"не удалось распарсить JSON из ответа LLM (len={len(s)})")
