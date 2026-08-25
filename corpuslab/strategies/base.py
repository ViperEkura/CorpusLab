"""PlanExecuteStrategy skeleton: Plan (diverse task sheets + deterministic ids)
→ Execute (concurrent filling).

- ids are derived at Plan time (docs/checkpoint-design.md §3) — "already
  finished?" is decidable before spending money;
- `_safe` bundles "call + parse": JSON parse failures get the same retry
  path as network failures;
- batch size is an engine-internal constant, never exposed as config
  (DESIGN §9.4).
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, List, Optional

from corpuslab.core.sample import Sample, TaskSpec

EXECUTE_BATCH = 8                    # engine-internal constant (not config)


class PlanExecuteStrategy:
    type = "base"

    def __init__(self, cfg: Any):
        self.cfg = cfg

    # ── Plan ──────────────────────────────────────────────
    async def plan(self, materials: AsyncIterator[Any], budget: int,
                   ctx: Any) -> AsyncIterator[TaskSpec]:
        async for spec in self._plan(materials, budget, ctx):
            if ctx.store is not None and not ctx.preview:
                ctx.store.mark_planned(spec)
            yield spec

    async def _plan(self, materials: AsyncIterator[Any], budget: int,
                    ctx: Any) -> AsyncIterator[TaskSpec]:
        raise NotImplementedError

    # ── Execute ───────────────────────────────────────────
    async def execute(self, specs: AsyncIterator[TaskSpec],
                      ctx: Any) -> AsyncIterator[Sample]:
        batch: List[TaskSpec] = []
        results: List[Optional[Sample]] = []

        async def flush():
            if not batch:
                return
            outcomes = await asyncio.gather(
                *(self._execute_one(s, ctx) for s in batch),
                return_exceptions=True)
            results.extend(outcomes)
            batch.clear()

        async for spec in specs:
            if spec.id in ctx.terminal:
                ctx.report.cache_hits += 1
                continue
            batch.append(spec)
            if len(batch) >= EXECUTE_BATCH:
                await flush()
        await flush()

        for res in results:
            if isinstance(res, BaseException):
                ctx.report.drop(self.type, f"execute_error:{type(res).__name__}")
                continue
            if res is not None:
                yield res

    async def _execute_one(self, spec: TaskSpec, ctx: Any) -> Optional[Sample]:
        raise NotImplementedError

    # ── Helpers ───────────────────────────────────────────
    def phases_params(self, phase: str, ctx: Any) -> dict:
        base = dict(ctx.cfg.llm.params or {})
        base.update((getattr(self.cfg, "phases", None) or {}).get(phase) or {})
        return base

    @staticmethod
    def make_sample(spec: TaskSpec, *, instruction: str, output: str = "",
                    reasoning: str = "", messages: list = None,
                    tools: list = None, extra_meta: dict = None) -> Sample:
        md = {"lineage": dict(spec.lineage)}
        if extra_meta:
            md.update(extra_meta)
        return Sample(id=spec.id, strategy=spec.strategy,
                      instruction=instruction, output=output,
                      reasoning=reasoning, messages=messages or [],
                      tools=tools or [], metadata=md)
