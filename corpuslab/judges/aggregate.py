"""Multi-judge aggregation, exactly per DESIGN.md §6.3:

1. Per dimension: remote judge scores aggregated by `aggregation`;
2. Local scorers emit [0,1] raw values, scaled to `dim.max × weight`,
   written into same-named dimensions (remote wins; local only annotates
   score_source);
3. total_score = Σ dimension values (absolute scale); min_total compares
   against the same scale;
4. min_judges / max_disagreement unmet → drop(insufficient_judges |
   judge_disagreement); min_total unmet → drop(min_total)."""
from __future__ import annotations

import statistics
from typing import Any, List, Optional

from corpuslab.core.sample import Sample, Score


class AggregateJudge:
    """Facade the engine talks to: fan-out over judges + scorers, then
    governance rules, then min_total."""

    def __init__(self, judge_cfg: Any, remote_judges: List[Any],
                 scorers: List[Any]):
        self.cfg = judge_cfg
        self.remote = remote_judges
        self.scorers = scorers

    def _agg(self, values: List[float], how: str) -> float:
        if not values:
            return 0.0
        return {"mean": lambda v: statistics.fmean(v),
                "min": min, "max": max,
                "median": lambda v: statistics.median(v)}[how](values)

    async def score(self, sample: Sample, ctx: Any) -> Optional[Score]:
        dims = {d.name: d for d in self.cfg.dimensions}

        # ── Fan out over remote judges ─────────────────────
        per_dim: dict = {name: [] for name in dims}
        success = 0
        for judge in self.remote:
            sc = await judge.score(sample, ctx)
            if sc and sc.scores:
                success += 1
                for name, v in sc.scores.items():
                    if name in per_dim:
                        per_dim[name].append(float(v))

        # min_judges gate
        if self.remote and success < self.cfg.min_judges:
            ctx.drop(sample, "judge", "insufficient_judges")
            return None

        # max_disagreement gate (per-dimension max spread among judges)
        md = self.cfg.max_disagreement
        if md and self.remote:
            for name, values in per_dim.items():
                if len(values) >= 2 and (max(values) - min(values)) > md:
                    ctx.drop(sample, "judge", "judge_disagreement")
                    return None

        # ── Aggregate remote scores per dimension ──────────
        final: dict = {}
        for name, values in per_dim.items():
            if values:
                final[name] = self._agg(values, self.cfg.aggregation)

        # ── Local scorers fill in (or annotate) dimensions ──
        sources = ["llm"] if self.remote else []
        for scorer in self.scorers:
            sc = await scorer.score(sample, ctx)
            if not sc:
                continue
            sources.append(sc.source)
            for name, raw in sc.scores.items():
                d = dims.get(name)
                if d is None:
                    continue
                scaled = float(raw) * float(d.max) * scorer.cfg.weight
                if name not in final:            # remote wins on same names
                    final[name] = scaled

        total = sum(final.values())
        if final:
            sample.metrics["scores"] = {k: round(v, 3) for k, v in final.items()}
            sample.metrics["total_score"] = round(total, 3)
            sample.metrics["score_source"] = "+".join(sources) or "none"

        # min_total gate
        if self.cfg.min_total and total < self.cfg.min_total:
            ctx.drop(sample, "judge", "min_total")
            return None
        return Score(sample_id=sample.id, scores=final, total=total,
                     source="+".join(sources) or "none")
