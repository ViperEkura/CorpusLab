"""semantic_dedup / cluster_dedup (batch): cosine gate, pending table
backpressure writes, embedding cache."""
from __future__ import annotations

import pytest

from corpuslab.core.store import Store
from corpuslab.stages.semantic import ClusterDedupStage, SemanticDedupStage
from tests.conftest import make_sample


class FakeCtx:
    def __init__(self, embed, store=None):
        from corpuslab.testing import FakeEmbedding
        from corpuslab.core.sample import RunReport
        self.report = RunReport()
        self.store = store
        self.preview = store is None
        self.embedding = embed or FakeEmbedding()
        self._pending = []

    def drop(self, sample, stage, reason):
        self.report.drop(stage, reason)

    async def embed(self, texts):
        return await self.embedding.embed(texts)

    def pending(self, sample):
        self._pending.append(sample.id)


def _identical(i: int) -> object:
    return make_sample(i, instruction=f"question {i}",
                       output="shared answer body " + " ".join(f"t{j}" for j in range(12)))


class ThresholdCfg:
    """tiny local shim so semantic threshold cfg reads .threshold"""

    def __init__(self, threshold=0.85):
        self.threshold = threshold


@pytest.mark.asyncio
async def test_semantic_dedup_drops_near_duplicates():
    stage = SemanticDedupStage(ThresholdCfg())
    a, b = _identical(0), _identical(1)          # same text → cos 1.0
    c = make_sample(2, instruction="other question",
                    output="completely different words here and there")
    out = await stage.apply_batch([a, b, c], FakeCtx(None))
    assert [s.id for s in out] == [a.id, c.id]


@pytest.mark.asyncio
async def test_cluster_dedup_keeps_anchor_drops_crowded():
    stage = ClusterDedupStage()
    a, b = _identical(0), _identical(1)
    c = make_sample(2, instruction="q", output="distinct unique content once more")
    out = await stage.apply_batch([a, b, c], FakeCtx(None))
    assert a in out and c in out
    assert b not in out                          # crowded-cluster dup of anchor


@pytest.mark.asyncio
async def test_pending_backpressure_written_via_pipeline_barrier(tmp_path):
    # The barrier (pipeline._barrier) spills every in-flight sample into the
    # pending table before apply_batch executes.
    from corpuslab.core.pipeline import Pipeline
    store = Store(str(tmp_path / "s.duckdb"))
    ctx = FakeCtx(None, store)
    stage = SemanticDedupStage(type("C", (), {"threshold": 0.85})())

    async def stream():
        yield _identical(0)
        yield _identical(1)

    survivors = []
    final = await Pipeline([stage])._barrier(stage, stream(), ctx)
    async for s in final:
        survivors.append(s)
    rows = store.conn.execute("SELECT id FROM pending").fetchall()
    store.clear_pending([r[0] for r in rows])     # barrier cleanup contract
    assert store.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert len(ctx._pending) == 2                 # both samples were spilled
    assert len(survivors) == 1
    store.close()

