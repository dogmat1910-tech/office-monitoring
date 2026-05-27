"""Retry-обёртка для LLM-запросов через OpenRouter.

Используется всеми модулями: classify_voice, auto_categorize,
classify_keystroke_text, analyze, analyze_conversation, daily_report.

Стратегия: 3 попытки, backoff 2→5→15 секунд. Ретраим на 429 (rate limit),
500/502/503 (OpenRouter down), таймаут. Не ретраим 400 (наш баг).
"""

from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Callable, TypeVar

import httpx

log = logging.getLogger("llm_retry")

T = TypeVar("T")

MAX_RETRIES = 3
BACKOFF_SECONDS = [2, 5, 15]
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def with_llm_retry(fn: Callable[..., T]) -> Callable[..., T]:
    """Декоратор: ретраит функцию при httpx-ошибках на retryable коды."""
    @wraps(fn)
    def wrapper(*args, **kwargs) -> T:
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code not in RETRYABLE_STATUS_CODES:
                    raise
                if attempt < MAX_RETRIES:
                    delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                    log.warning("LLM %d/%d: HTTP %d, retry in %ds",
                                attempt + 1, MAX_RETRIES, e.response.status_code, delay)
                    time.sleep(delay)
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                    log.warning("LLM %d/%d: %s, retry in %ds",
                                attempt + 1, MAX_RETRIES, type(e).__name__, delay)
                    time.sleep(delay)
        raise last_exc  # type: ignore[misc]
    return wrapper
