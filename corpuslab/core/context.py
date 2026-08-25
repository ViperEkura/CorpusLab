"""RunContext: the full environment of one run (seed, endpoints, infra, report)."""
from __future__ import annotations

import random
from typing import Any, Optional

from corpuslab.core.sample import RunReport, Sample
from corpuslab.core.store import Store


class RunContext:
    """Plugins must not build their own infrastructure — everything goes
    through ctx (S1/S4)."""

    def __init__(self, cfg: Any, *, store: Optional[Store] = None,
                 llm: Any = None, embedding: Any = None,
                 report: Optional[RunReport] = None,
                 preview: bool = False):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.embedding = embedding
        self.report = report or RunReport()
        self.preview = preview
        seed = getattr(getattr(cfg, "run", None), "seed", None)
        self.rng = random.Random(seed)
        self.lang = getattr(getattr(cfg, "llm", None), "lang", "en")
        # Populated by the engine before execution: terminal ids from resume
        self.terminal: set = set()

    # ── Stage-decision helpers (drop and state land in one transaction) ──
    def drop(self, sample: Sample, stage: str, reason: str) -> None:
        self.report.drop(stage, reason)
        if self.store is not None and not self.preview:
            self.store.drop_sample(sample.id, sample.strategy, stage, reason)

    def pending(self, sample: Sample) -> None:
        """Disk-backpressure write issued before the batch barrier."""
        if self.store is not None and not self.preview:
            self.store.save_pending(sample)

    def event(self, t: str, sid: str = "", strategy: str = "",
              data: Optional[dict] = None) -> None:
        if self.store is not None and not self.preview:
            self.store.event(t, sid, strategy, data)

    # ── LLM/embedding convenience entry points (may be fakes) ──
    async def chat(self, messages: list, *, endpoint: Optional[str] = None,
                   params: Optional[dict] = None) -> str:
        if self.llm is None:
            raise RuntimeError("RunContext has no LLM client attached")
        self.report.llm_calls += 1
        return await self.llm.chat(messages, endpoint=endpoint, params=params)

    async def embed(self, texts: list) -> list:
        if self.embedding is None:
            raise RuntimeError(
                "RunContext has no embedding client attached "
                "(semantic_dedup/cluster_dedup/semantic chunking need the "
                "embedding section)")
        return await self.embedding.embed(texts)

    # ── Convenience: three-layer params merge for a phase ──
    def phase_params(self, base_params: dict, phases: dict, phase: str) -> dict:
        out = dict(base_params or {})
        out.update((phases or {}).get(phase) or {})
        return out
