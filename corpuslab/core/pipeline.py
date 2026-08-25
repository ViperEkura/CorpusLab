"""Pipeline engine: single global instance, streaming-group chaining, batch
barrier with pending-table backpressure.

Every strategy's execute feeds into the same pipeline — dedup state is shared
across strategies (DESIGN.md §5.2: running the pipeline once per strategy
would let cross-strategy duplicates slip through).
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, List

from corpuslab.core.sample import Sample

log = logging.getLogger("corpuslab.pipeline")


class Pipeline:
    """Chain stages in declared order; users declare order only, never
    scheduling (the form is decided by which protocol the stage implements)."""

    def __init__(self, stages: List[Any]):
        self.stages = stages
        for s in self.stages:
            if not hasattr(s, "type"):
                raise ValueError(f"stage missing type: {s!r}")

    def describe(self) -> str:
        kinds = []
        for s in self.stages:
            kind = "batch" if hasattr(s, "apply_batch") else "streaming"
            kinds.append(f"{s.type}({kind})")
        return " → ".join(kinds) or "(empty)"

    async def run(self, stream: AsyncIterator[Sample], ctx: Any) -> AsyncIterator[Sample]:
        current = stream
        for stage in self.stages:
            if hasattr(stage, "apply_batch"):
                current = await self._barrier(stage, current, ctx)
            else:
                current = stage.apply_stream(current, ctx)
        async for sample in current:
            yield sample

    async def _barrier(self, stage: Any, stream: AsyncIterator[Sample],
                       ctx: Any) -> AsyncIterator[Sample]:
        """Batch barrier: spill in-flight samples to the pending table (disk
        backpressure), then execute once.

        The buffer is a disk queue from day one, not an in-memory list —
        which dissolves the barrier-vs-crash conflict (DESIGN.md §5.2).
        """
        buffered: List[Sample] = []
        async for sample in stream:
            ctx.pending(sample)                 # disk backpressure
            buffered.append(sample)
        log.info("batch barrier %s: %d samples to process", stage.type, len(buffered))
        survivors = await stage.apply_batch(buffered, ctx)

        async def _gen():
            for s in survivors:
                yield s
        return _gen()
