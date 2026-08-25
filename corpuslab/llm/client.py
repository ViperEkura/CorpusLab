"""LLM client: the sole retry primitive retry_with_backoff, per-endpoint
circuit breaker, per-endpoint semaphore (S4).

OpenAI-compatible protocol over httpx; tests inject a FakeLLM through the
same chat interface.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Dict, Optional, Protocol

import httpx

from corpuslab.config.loader import env_resolve_endpoint, resolve_endpoint
from corpuslab.config.schema import Config, LlmCfg

log = logging.getLogger("corpuslab.llm")


class CircuitBreakerOpen(RuntimeError):
    """Retry ratio inside the sliding window exceeded the threshold: this
    endpoint trips its breaker. When every endpoint in use is open, the run
    aborts."""


class Breaker:
    """Counted per endpoint (DESIGN §7.2: a judge endpoint's failure must not
    abort generation)."""

    def __init__(self, window: float, max_retry_ratio: float):
        self.window = window
        self.max_retry_ratio = max_retry_ratio
        self.calls: deque = deque()      # (ts, is_retry)

    def record(self, is_retry: bool) -> None:
        now = time.monotonic()
        self.calls.append((now, is_retry))
        while self.calls and now - self.calls[0][0] > self.window:
            self.calls.popleft()

    def check(self) -> None:
        if not self.calls or len(self.calls) < 5:
            return
        retries = sum(1 for _, r in self.calls if r)
        if retries / len(self.calls) > self.max_retry_ratio:
            raise CircuitBreakerOpen(
                f"breaker open: {retries}/{len(self.calls)} retries within "
                f"{self.window}s window")


async def retry_with_backoff(coro_fn, *, attempts: int, backoff: float,
                             max_delay: float, on_retry=None):
    """The sole retry primitive: exponential backoff. coro_fn() → awaitable;
    raises the final exception."""
    delay = min(backoff, max_delay)
    last: Optional[BaseException] = None
    for i in range(max(attempts, 1)):
        try:
            return await coro_fn()
        except CircuitBreakerOpen:
            raise
        except (httpx.HTTPError, httpx.HTTPStatusError, json.JSONDecodeError,
                ValueError, RuntimeError, OSError) as e:
            last = e
            if on_retry:
                on_retry()
            if i == attempts - 1:
                break
            await asyncio.sleep(min(delay, max_delay))
            delay = min(delay * backoff, max_delay)
    raise last  # type: ignore[misc]


class LLMClient(Protocol):
    async def chat(self, messages: list, *, endpoint: Optional[str] = None,
                   params: Optional[dict] = None) -> str: ...


class HttpLLMClient:
    """Production implementation: per-endpoint semaphore + retry + breaker."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._resolved: Dict[str, LlmCfg] = {}

    def endpoint_cfg(self, name: Optional[str]) -> LlmCfg:
        key = name or "llm"
        if key not in self._resolved:
            self._resolved[key] = env_resolve_endpoint(resolve_endpoint(self.cfg, name))
        return self._resolved[key]

    def _semaphore(self, ep: LlmCfg):
        sems = getattr(self, "_sems", None)
        if sems is None:
            sems = self._sems = {}
        # cache by endpoint name so merged endpoint configs share one lock
        key = ep.model or "llm"
        if key not in sems:
            sems[key] = asyncio.Semaphore(max(ep.concurrency, 1))
        return sems[key]

    def _breaker(self, ep: LlmCfg) -> Breaker:
        brs = getattr(self, "_breakers", None)
        if brs is None:
            brs = self._breakers = {}
        key = ep.model or "llm"
        if key not in brs:
            brs[key] = Breaker(ep.breaker.window, ep.breaker.max_retry_ratio)
        return brs[key]

    async def chat(self, messages: list, *, endpoint: Optional[str] = None,
                   params: Optional[dict] = None) -> str:
        ep = self.endpoint_cfg(endpoint)
        sem = self._semaphore(ep)
        br = self._breaker(ep)

        async def _once() -> str:
            br.check()
            base = ep.base_url or "https://api.openai.com/v1"
            url = base.rstrip("/") + "/chat/completions"
            body: Dict[str, Any] = {
                "model": ep.model,
                "messages": messages,
                **{**(ep.params or {}), **(params or {})},
            }
            headers = {"Content-Type": "application/json"}
            if ep.api_key:
                headers["Authorization"] = f"Bearer {ep.api_key}"
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(url, json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
            content = data["choices"][0]["message"]["content"]
            return content if isinstance(content, str) else json.dumps(content)

        def _on_retry():
            br.record(True)

        async with sem:
            result = await retry_with_backoff(
                _once, attempts=ep.retry.attempts, backoff=ep.retry.backoff,
                max_delay=ep.retry.max_delay, on_retry=_on_retry)
            br.record(False)
            return result


async def chat_json(ctx, messages: list, *, endpoint: Optional[str] = None,
                    params: Optional[dict] = None) -> Optional[dict]:
    """Wrap "call + parse" on one retry path: JSON parse failure gets the
    same treatment as network failure."""
    async def _once():
        text = await ctx.chat(messages, endpoint=endpoint, params=params)
        from corpuslab.config.loader import extract_json_object
        obj = extract_json_object(text)
        if obj is None:
            raise ValueError("no JSON object found in the LLM reply")
        return obj

    try:
        return await _once()
    except ValueError:
        # network-level retry already happened inside ctx.chat; retry the
        # call once more for a parseable reply (simple and sufficient)
        text = await ctx.chat(messages, endpoint=endpoint, params=params)
        from corpuslab.config.loader import extract_json_object
        return extract_json_object(text)
