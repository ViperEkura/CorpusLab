"""exact_dedup stage: SHA256 drops, fingerprint persistence in the store,
resume behavior."""
from __future__ import annotations

import pytest

from corpuslab.core.store import Store
from corpuslab.stages.exact_dedup import ExactDedupStage
from tests.conftest import make_sample


class FakeCtx:
    def __init__(self, store=None):
        from corpuslab.core.sample import RunReport
        self.report = RunReport()
        self.store = store
        self.preview = store is None

    def drop(self, sample, stage, reason):
        self.report.drop(stage, reason)


async def _collect(stage, samples, ctx):
    async def stream():
        for s in samples:
            yield s
    return [s async for s in stage.apply_stream(stream(), ctx)], ctx.report


@pytest.mark.asyncio
async def test_duplicates_dropped_kept_unique():
    stage = ExactDedupStage(None)
    a = make_sample(0, instruction="same text", output="same out")
    b = make_sample(1, instruction="same text", output="same out")
    c = make_sample(2, instruction="different", output="other")
    out, report = await _collect(stage, [a, b, c], FakeCtx())
    assert out == [a, c]
    assert report.dropped["exact_dedup"]["duplicate"] == 1


@pytest.mark.asyncio
async def test_fingerprints_persist_and_resume_drops(tmp_path):
    db = str(tmp_path / "s.duckdb")
    store = Store(db)
    stage1 = ExactDedupStage(None)
    a = make_sample(0, instruction="same text", output="same out")
    ctx1 = FakeCtx(store)
    out, _ = await _collect(stage1, [a], ctx1)
    assert out == [a]
    assert store.fingerprint_count() == 1
    store.close()

    # New process: fresh stage + reopened store → duplicate must drop again
    store2 = Store(db)
    stage2 = ExactDedupStage(None)
    b = make_sample(99, instruction="same text", output="same out")
    ctx2 = FakeCtx(store2)
    out2, report2 = await _collect(stage2, [b], ctx2)
    assert not out2
    assert report2.dropped["exact_dedup"]["duplicate(resume)"] == 1
    store2.close()


@pytest.mark.asyncio
async def test_preview_mode_does_not_touch_store(tmp_path):
    # preview=True path (store None): no persistence errors, pure memory
    stage = ExactDedupStage(None)
    a = make_sample(0, instruction="x", output="y")
    b = make_sample(1, instruction="x", output="y")
    out, _ = await _collect(stage, [a, b], FakeCtx(None))
    assert len(out) == 1
