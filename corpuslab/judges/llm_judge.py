"""LLM-as-Judge: dimension prompt construction, JSON score parsing (retries
via the shared llm/client path; results cached per endpoint in the store)."""
from __future__ import annotations

from typing import Any, List, Optional

from corpuslab.config.loader import extract_json_object
from corpuslab.core.sample import Sample, Score


class LLMJudge:
    """Single-endpoint judge. Multi-judge governance (aggregation,
    min_judges, max_disagreement) lives in judges/aggregate.py."""

    def __init__(self, endpoint: Optional[str] = None,
                 dimensions: Optional[List[Any]] = None,
                 lang: str = "en"):
        self.endpoint = endpoint or "llm"
        self.dimensions = dimensions or []
        self.lang = lang

    def _prompt(self, sample: Sample) -> list:
        dims = "; ".join(f"{d.name} (1..{int(d.max)}, {d.label or d.name})"
                         for d in self.dimensions)
        if sample.messages:
            body = "\n".join(f"[{m.get('role')}] {m.get('content')}"
                             for m in sample.messages)
        else:
            body = f"instruction: {sample.instruction}\noutput: {sample.output}"
        zh = self.lang == "zh"
        system = ("You are a strict data quality judge. "
                  + ("请只返回 JSON。" if zh else "Return JSON only."))
        user = (f"Score the sample on each dimension (1 = worst, max = best).\n"
                f"Dimensions: {dims}\n\nSample:\n{body}\n\n"
                + ("返回 JSON：{\"scores\": {\"dim\": n, ...}}"
                   if zh else 'Return JSON: {"scores": {"dim": n, ...}}'))
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    async def score(self, sample: Sample, ctx: Any) -> Score:
        # Cache hit for this (sample, endpoint) pair → skip the call
        if ctx.store is not None and not ctx.preview:
            cached = ctx.store.load_score(sample.id, self.endpoint)
            if cached is not None:
                ctx.report.cache_hits += 1
                return Score(sample_id=sample.id, scores=cached["scores"],
                             total=cached["total"], source="llm",
                             endpoint=self.endpoint)

        obj = extract_json_object(
            await ctx.chat(self._prompt(sample), endpoint=self.endpoint))
        scores: dict = {}
        if obj and isinstance(obj.get("scores"), dict):
            for d in self.dimensions:
                v = obj["scores"].get(d.name)
                if v is None:
                    continue
                try:
                    scores[d.name] = max(1.0, min(float(v), float(d.max)))
                except (TypeError, ValueError):
                    continue
        total = sum(scores.values())
        if ctx.store is not None and not ctx.preview:
            ctx.store.save_score(sample.id, self.endpoint, scores, total)
        return Score(sample_id=sample.id, scores=scores, total=total,
                     source="llm", endpoint=self.endpoint)
