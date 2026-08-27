"""PlanExecuteStrategy skeleton: Plan (diverse task sheets + deterministic ids)
→ Execute (concurrent filling).

- ids are derived at Plan time (docs/checkpoint-design.md §3) — "already
  finished?" is decidable before spending money;
- `_safe` bundles "call + parse": JSON parse failures get the same retry
  path as network failures (§7) — the sole primitive retry_with_backoff
  applies to both;
- a tripped circuit breaker aborts the whole run (exit 3), it must not be
  swallowed as a per-sample drop;
- batch size is an engine-internal constant, never exposed as config
  (DESIGN §9.4).
"""

from __future__ import annotations
__all__ = [
    "PlanExecuteStrategy",
    "EXECUTE_BATCH",
]

import asyncio
from typing import Any, AsyncIterator, List, Optional

from corpuslab.core.sample import Sample, TaskSpec
from corpuslab.llm.client import CircuitBreakerOpen, chat_json

EXECUTE_BATCH = 8                    # engine-internal constant (not config)


class PlanExecuteStrategy:
    type = "base"

    def __init__(self, cfg: Any):
        self.cfg = cfg

    async def plan(self, materials: AsyncIterator[Any], budget: int,
                   ctx: Any) -> AsyncIterator[TaskSpec]:
        async for spec in self._plan(materials, budget, ctx):
            if ctx.store is not None and not ctx.preview:
                ctx.store.mark_planned(spec)
            yield spec

    async def _plan(self, materials: AsyncIterator[Any], budget: int,
                    ctx: Any) -> AsyncIterator[TaskSpec]:
        raise NotImplementedError

    # Execute: gather outcomes per fixed-size batch, then resolve after all
    # plans land (breakers must abort, parse failures drop).
    async def execute(self, specs: AsyncIterator[TaskSpec],
                      ctx: Any) -> AsyncIterator[Sample]:
        batch: List[TaskSpec] = []
        results: List[tuple] = []                 # (spec, outcome)

        async def flush():
            if not batch:
                return
            outcomes = await asyncio.gather(
                *(self._execute_one(s, ctx) for s in batch),
                return_exceptions=True)
            results.extend(zip(list(batch), outcomes))
            batch.clear()

        async for spec in specs:
            if spec.id in ctx.terminal:
                ctx.report.cache_hits += 1
                continue
            batch.append(spec)
            if len(batch) >= EXECUTE_BATCH:
                await flush()
        await flush()

        for spec, res in results:
            if isinstance(res, BaseException):
                if isinstance(res, CircuitBreakerOpen):
                    raise res                     # breaker trips → abort run (exit 3)
                # Record execute failures as terminal drops in the store too —
                # otherwise the id is neither committed nor dropped and resume
                # would re-spend LLM calls on it.
                reason = f"execute_error:{type(res).__name__}"
                ctx.report.drop(self.type, reason)
                if ctx.store is not None and not ctx.preview:
                    ctx.store.drop_sample(spec.id, spec.strategy, self.type, reason)
                continue
            if res is not None:
                yield res

    async def _execute_one(self, spec: TaskSpec, ctx: Any) -> Optional[Sample]:
        raise NotImplementedError

    # Unified "call + parse" helper: one retry path for both.
    async def _safe(self, ctx: Any, messages: list, *,
                    endpoint: Optional[str] = None,
                    params: Optional[dict] = None):
        return await chat_json(ctx, messages, endpoint=endpoint, params=params)

    # Helpers
    def phases_params(self, phase: str, ctx: Any) -> dict:
        base = dict(ctx.cfg.llm.params or {})
        base.update((getattr(self.cfg, "phases", None) or {}).get(phase) or {})
        return base

    @staticmethod
    def make_sample(spec: TaskSpec, *, instruction: str = "", output: str = "",
                    reasoning: str = "", messages: list = None,
                    tools: list = None, extra_meta: dict = None) -> Sample:
        md = {"lineage": dict(spec.lineage)}
        if extra_meta:
            md.update(extra_meta)
        return Sample(id=spec.id, strategy=spec.strategy,
                      instruction=instruction, output=output,
                      reasoning=reasoning, messages=messages or [],
                      tools=tools or [], metadata=md)
