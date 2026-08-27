"""stats stage: special chars, repetition, n-gram diversity thresholds."""
from __future__ import annotations

import pytest

from corpuslab.config import schema as S
from corpuslab.stages.stats import (StatsStage, ngram_diversity,
                                    repetition_ratio, special_char_ratio)
from tests.conftest import make_sample


class FakeCtx:
    def __init__(self):
        from corpuslab.core.sample import RunReport
        self.report = RunReport()
        self.store = None
        self.preview = True

    def drop(self, sample, stage, reason):
        self.report.drop(stage, reason)


async def _collect(stage, samples):
    async def stream():
        for s in samples:
            yield s
    ctx = FakeCtx()
    out = [s async for s in stage.apply_stream(stream(), ctx)]
    return out, ctx.report


def test_unit_functions():
    assert special_char_ratio("") == 0.0
    assert special_char_ratio("abc!!") == pytest.approx(2 / 5)
    assert repetition_ratio("aaaa", "char", "char") == 1.0
    # char mode = longest run / total: "abab" has no repeats → 1/4
    assert repetition_ratio("abab", "char", "char") == 0.25
    assert ngram_diversity("abcd", 3, "char") == 1.0
    # text shorter than n → treated as fully diverse
    assert ngram_diversity("ab", 3, "char") == 1.0


@pytest.mark.asyncio
async def test_special_char_gate():
    stage = StatsStage(S.StatsStageCfg(type="stats", max_special_char_ratio=0.1))
    bad = make_sample(0, instruction="!!!!! noise", output="!!!!! more")
    good = make_sample(1, instruction="clean plain words here", output="answer body")
    out, report = await _collect(stage, [bad, good])
    assert out == [good]
    k = next(k for k in report.dropped["stats"] if k.startswith("special_char_ratio"))
    assert report.dropped["stats"][k] == 1


@pytest.mark.asyncio
async def test_ngram_diversity_drop_and_kept_by():
    stage = StatsStage(S.StatsStageCfg(type="stats", min_ngram_diversity=0.9))
    repetitive = make_sample(0, instruction="AAAAAAAAAA", output="BBBBBBBBBB")
    diverse = make_sample(1, instruction="words one two three four five",
                          output="more varied content follows here now")
    out, _ = await _collect(stage, [repetitive, diverse])
    assert out == [diverse]
    assert "stats" in diverse.metrics["kept_by"]
    assert "ngram_diversity" in diverse.metrics["stats"]
