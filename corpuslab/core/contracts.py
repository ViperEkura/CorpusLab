"""The five core Protocols (S2: protocols live in core, implementations in plugin packages).

Streaming and batch are **two separate** stage protocols — batch is async (it needs
to call the embedding client); there is no fat interface forcing both entry
points (docs/project-structure.md §4).
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable

from corpuslab.core.sample import RunReport  # noqa: F401  (re-export)
from corpuslab.core.sample import Sample, Score, TaskSpec


@runtime_checkable
class Material(Protocol):
    """Unified read-only view over raw materials."""

    kind: str          # "topic" | "seed" | "document" | "tool" | "file"
    payload: dict


@runtime_checkable
class Source(Protocol):
    kind: str

    def open(self, cfg: Any, ctx: Any) -> AsyncIterator[Any]: ...


@runtime_checkable
class Strategy(Protocol):
    type: str

    async def plan(self, materials: AsyncIterator[Any], budget: int,
                   ctx: Any) -> AsyncIterator[TaskSpec]: ...

    async def execute(self, specs: AsyncIterator[TaskSpec],
                      ctx: Any) -> AsyncIterator[Sample]: ...


@runtime_checkable
class StreamingStage(Protocol):
    type: str

    async def apply_stream(self, stream: AsyncIterator[Sample],
                           ctx: Any) -> AsyncIterator[Sample]: ...


@runtime_checkable
class BatchStage(Protocol):
    type: str

    async def apply_batch(self, samples: list[Sample],
                          ctx: Any) -> list[Sample]: ...


@runtime_checkable
class Judge(Protocol):
    async def score(self, sample: Sample, ctx: Any) -> Score: ...


@runtime_checkable
class Sink(Protocol):
    async def write(self, stream: AsyncIterator[Sample], ctx: Any) -> Any: ...
