"""Judge aggregation tests (DESIGN §6.3 semantics)."""
from __future__ import annotations

import pytest

from corpuslab.core.context import RunContext
from corpuslab.judges.aggregate import AggregateJudge
from tests.conftest import make_config, make_sample


class _FixedJudge:
    """Remote judge stub emitting fixed per-dimension scores."""

    def __init__(self, endpoint: str, table: dict):
        self.endpoint = endpoint
        self.table = table

    async def score(self, sample, ctx):
        from corpuslab.core.sample import Score
        scores = {k: v for k, v in self.table.items()}
        return Score(sample_id=sample.id, scores=scores,
                     total=sum(scores.values()), source="llm",
                     endpoint=self.endpoint)


def _cfg_with_judge(tmp_path, **kw):
    cfg = make_config(tmp_path)
    cfg.judge.dimensions = [
        type("D", (), {"name": "correctness", "label": "A", "max": 10})(),
        type("D", (), {"name": "helpfulness", "label": "B", "max": 10})(),
    ]
    for k, v in kw.items():
        setattr(cfg.judge, k, v)
    return cfg


@pytest.mark.asyncio
async def test_mean_aggregation(tmp_path):
    cfg = _cfg_with_judge(tmp_path, aggregation="mean", min_total=0,
                          min_judges=1)
    j = AggregateJudge(cfg.judge, [
        _FixedJudge("pro", {"correctness": 8, "helpfulness": 6}),
        _FixedJudge("flash", {"correctness": 4, "helpfulness": 8}),
    ], [])
    ctx = RunContext(cfg)
    sc = await j.score(make_sample(1), ctx)
    assert sc.scores["correctness"] == pytest.approx(6.0)   # mean(8,4)
    assert sc.scores["helpfulness"] == pytest.approx(7.0)   # mean(6,8)
    assert sc.total == pytest.approx(13.0)


@pytest.mark.asyncio
async def test_min_judges_drop(tmp_path):
    cfg = _cfg_with_judge(tmp_path, min_judges=2)
    failing = _FixedJudge("flash", {})                      # no scores → not a success
    j = AggregateJudge(cfg.judge, [_FixedJudge("pro", {"correctness": 8}),
                                   failing], [])
    ctx = RunContext(cfg)
    sc = await j.score(make_sample(2), ctx)
    assert sc is None
    assert "insufficient_judges" in ctx.report.dropped.get("judge", {})


@pytest.mark.asyncio
async def test_disagreement_drop(tmp_path):
    cfg = _cfg_with_judge(tmp_path, max_disagreement=2, min_judges=1)
    j = AggregateJudge(cfg.judge, [
        _FixedJudge("pro", {"correctness": 9}),
        _FixedJudge("flash", {"correctness": 3}),           # spread 6 > 2
    ], [])
    ctx = RunContext(cfg)
    sc = await j.score(make_sample(3), ctx)
    assert sc is None
    assert "judge_disagreement" in ctx.report.dropped.get("judge", {})


@pytest.mark.asyncio
async def test_min_total_drop(tmp_path):
    cfg = _cfg_with_judge(tmp_path, min_total=15, min_judges=1)
    j = AggregateJudge(cfg.judge, [
        _FixedJudge("pro", {"correctness": 5, "helpfulness": 5}),  # 10 < 15
    ], [])
    ctx = RunContext(cfg)
    sc = await j.score(make_sample(4), ctx)
    assert sc is None
    assert "min_total" in ctx.report.dropped.get("judge", {})


@pytest.mark.asyncio
async def test_local_scorer_fills_unjudged_dim(tmp_path):
    class _Local:
        def __init__(self, dims):
            self.cfg = type("C", (), {"weight": 1.0})()
            from corpuslab.core.sample import Score
            self._score = Score(sample_id="", scores=dims,
                                total=sum(dims.values()), source="fasttext")

        async def score(self, sample, ctx):
            from corpuslab.core.sample import Score
            return Score(sample_id=sample.id, scores=self._score.scores,
                         total=self._score.total, source="fasttext")

    cfg = _cfg_with_judge(tmp_path, min_total=0, min_judges=1)
    j = AggregateJudge(cfg.judge, [
        _FixedJudge("pro", {"correctness": 8}),             # only correctness
    ], [_Local({"helpfulness": 0.8})])                      # scaled 0.8*10 = 8
    ctx = RunContext(cfg)
    sc = await j.score(make_sample(5), ctx)
    assert sc.scores["correctness"] == 8
    assert sc.scores["helpfulness"] == pytest.approx(8.0)
    assert sc.total == pytest.approx(16.0)
