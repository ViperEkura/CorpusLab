"""LLM client: the sole retry primitive retry_with_backoff, per-endpoint
circuit breaker, per-endpoint semaphore (S4).

OpenAI-compatible protocol over httpx on a pooled connection; tests inject a
FakeLLM through the same chat interface.
"""
from __future__ import annotations

__all__ = [
    "CircuitBreakerOpen",
    "Breaker",
    "retry_with_backoff",
    "LLMClient",
    "HttpLLMClient",
]

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Dict, Optional, Protocol

import httpx

from corpuslab.config.loader import extract_json_object
from corpuslab.llm.endpoints import EndpointResolver
from corpuslab.config.schema import LlmCfg

log = logging.getLogger("corpuslab.llm")


class CircuitBreakerOpen(RuntimeError):
    """Retry ratio inside the sliding window exceeded the threshold: this
    endpoint trips its breaker. When every endpoint in use is open, the run
    aborts."""


class Breaker:
    """Counted per endpoint NAME (DESIGN §7.2: a judge endpoint's failure must
    not abort generation; two endpoints sharing a model still count apart)."""

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
    """Production implementation: pooled httpx session, per-endpoint-name
    semaphore + retry + breaker."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.resolver = EndpointResolver(cfg)
        self._sems: Dict[str, asyncio.Semaphore] = {}
        self._breakers: Dict[str, Breaker] = {}
        self._http: Optional[httpx.AsyncClient] = None   # created in-loop

    def endpoint_cfg(self, name: Optional[str]) -> LlmCfg:
        return self.resolver.resolve(name).cfg

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _session(self) -> httpx.AsyncClient:
        if self._http is None:                    # lazy: needs a running loop
            self._http = httpx.AsyncClient(timeout=120)
        return self._http

    def _semaphore(self, ep_name: str, cfg: LlmCfg) -> asyncio.Semaphore:
        # keyed by endpoint name so concurrency contracts hold independently
        # of model identity (§6: semaphores are held per endpoint)
        if ep_name not in self._sems:
            self._sems[ep_name] = asyncio.Semaphore(max(cfg.concurrency, 1))
        return self._sems[ep_name]

    def _breaker(self, ep_name: str, cfg: LlmCfg) -> Breaker:
        # keyed by endpoint name (§6: breakers are counted per endpoint)
        if ep_name not in self._breakers:
            self._breakers[ep_name] = Breaker(cfg.breaker.window,
                                              cfg.breaker.max_retry_ratio)
        return self._breakers[ep_name]

    async def chat(self, messages: list, *, endpoint: Optional[str] = None,
                   params: Optional[dict] = None) -> str:
        resolved = self.resolver.resolve(endpoint)
        ep_name, ep = resolved.name, resolved.cfg
        sem = self._semaphore(ep_name, ep)
        br = self._breaker(ep_name, ep)
        client = self._session()

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
                    params: Optional[dict] = None) -> dict | None:
    """Wrap "call + parse" on ONE retry path (§7: parse failures take the
    same backoff path as network failures). Backoff parameters come from the
    endpoint config when resolvable, else global defaults."""
    attempts, backoff, max_delay = 3, 2.0, 30.0
    endpoint_cfg = getattr(getattr(ctx, "llm", None), "endpoint_cfg", None)
    if endpoint_cfg is not None:
        try:
            rcfg = endpoint_cfg(endpoint)
            attempts, backoff, max_delay = (rcfg.retry.attempts,
                                            rcfg.retry.backoff,
                                            rcfg.retry.max_delay)
        except Exception:                        # pragma: no cover
            pass

    async def _once():
        text = await ctx.chat(messages, endpoint=endpoint, params=params)
        obj = extract_json_object(text)
        if obj is None:
            raise ValueError("no JSON object found in the LLM reply")
        return obj

    try:
        return await retry_with_backoff(_once, attempts=attempts,
                                        backoff=backoff, max_delay=max_delay)
    except ValueError:
        return None                              # exhausted → caller drops
