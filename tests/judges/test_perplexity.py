"""perplexity scorer: logprob parsing, NLL math, quality mapping, mode
behavior and error surfacing (route A)."""
from __future__ import annotations

import pytest

from corpuslab.config.schema import PerplexityScorerCfg
from corpuslab.judges.perplexity import (PerplexityScorer, mean_token_nll,
                                         parse_prompt_logprobs, ppl_to_quality)


class Ctx:
    def __init__(self):
        from corpuslab.core.sample import RunReport
        self.report = RunReport()
        self.store = None
        self.preview = True

    def drop(self, sample, stage, reason):
        self.report.drop(stage, reason)


def test_parse_prompt_logprobs_vllm_shape():
    payload = {"choices": [{"logprobs": {"prompt_logprobs": [
        None,
        {"123": -0.5},
        {"77": -1.25},
    ]}}]}
    plp = parse_prompt_logprobs(payload)
    assert plp == [None, -0.5, -1.25]
    assert mean_token_nll(plp) == pytest.approx((0.5 + 1.25) / 2)


def test_parse_prompt_logprobs_missing():
    # DeepSeek-style gateway silently ignores prompt_logprobs
    assert parse_prompt_logprobs({"choices": [{"logprobs": {}}]}) is None
    assert parse_prompt_logprobs({"choices": []}) is None
    assert parse_prompt_logprobs({}) is None


def test_mean_token_nll_empty():
    assert mean_token_nll([]) is None
    assert mean_token_nll([None, None]) is None


def test_quality_mapping_bounds():
    assert ppl_to_quality(0.0) == 1.0
    assert 0.0 < ppl_to_quality(2.0) < 1.0
    assert ppl_to_quality(6.0) == 0.0                 # ceiling
    assert ppl_to_quality(50.0) == 0.0                # clamped
    assert ppl_to_quality(float("nan")) == 0.0


def _make_scorer(mode="continuation", **kw):
    cfg = PerplexityScorerCfg(type="perplexity", mode=mode, **kw)
    return PerplexityScorer(cfg, model="m", base_url="http://x/v1",
                            api_key="k")


@pytest.mark.asyncio
async def test_continuation_scores_mean_nll(monkeypatch):
    sc = _make_scorer()

    async def fake(self, client, body):
        return {"choices": [{"logprobs": {
            "tokens": ["a", "b"],
            "token_logprobs": [-1.0, -3.0],
        }}]}

    monkeypatch.setattr(PerplexityScorer, "_post_completions", fake)
    from tests.conftest import make_sample
    score = await sc.score(make_sample(0), Ctx())
    # mean nll = 2.0 → quality = 1 - 2/6 = 0.6667
    assert score.scores["ppl_quality"] == pytest.approx(0.6667, abs=1e-3)
    assert score.source == "perplexity"


@pytest.mark.asyncio
async def test_teacher_forced_requires_prompt_logprobs(monkeypatch):
    sc = _make_scorer("teacher_forced")

    async def fake(self, client, body):
        return {"choices": [{"logprobs": {}}]}       # gateway ignored it

    monkeypatch.setattr(PerplexityScorer, "_post_completions", fake)
    from tests.conftest import make_sample
    ctx = Ctx()
    score = await sc.score(make_sample(0), ctx)
    # scorer outage → no dimension scores, drop recorded for observability
    assert not score.scores
    assert any("scorer_error" in r for rs in ctx.report.dropped.values()
               for r in rs)


@pytest.mark.asyncio
async def test_custom_dimension_and_ceiling(monkeypatch):
    sc = _make_scorer(dimensions=["fluency"], nll_ceiling=4.0)

    async def fake(self, client, body):
        return {"choices": [{"logprobs": {"token_logprobs": [-2.0]}}]}

    monkeypatch.setattr(PerplexityScorer, "_post_completions", fake)
    from tests.conftest import make_sample
    score = await sc.score(make_sample(0), Ctx())
    assert score.scores["fluency"] == pytest.approx(0.5)   # 1 - 2/4
    assert score.total == pytest.approx(0.5)


def test_body_uses_completions_path(monkeypatch):
    import asyncio

    seen = {}

    async def fake_post(self, url, json=None, headers=None):
        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"logprobs": {
                    "token_logprobs": [-1.0]}}]}
        seen["url"] = url
        seen["body"] = json
        return R()

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    cfg = PerplexityScorerCfg(type="perplexity", model="m")
    sc = PerplexityScorer(cfg, model="m", base_url="http://x/v1")

    from tests.conftest import make_sample

    async def run():
        return await sc._score_continuation(make_sample(0).core_text())

    nll = asyncio.run(run())
    assert seen["url"].endswith("/completions")
    assert seen["body"]["model"] == "m"
    assert nll == 1.0
