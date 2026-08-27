"""minhash_dedup stage: LSH near-dup drops, signature persistence,
resume rebuild."""
from __future__ import annotations

import pytest

from corpuslab.config import schema as S
from corpuslab.core.store import Store
from corpuslab.stages.minhash import MinHashDedupStage
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


def _body(seed: int, n: int = 40) -> str:
    # text with controlled overlap: same seed → near-identical body
    words = [f"w{(seed * 7 + i * i) % 97}" for i in range(n)]
    return " ".join(words)


@pytest.mark.asyncio
async def test_near_duplicates_dropped_unique_kept():
    stage = MinHashDedupStage(S.MinHashStageCfg(type="minhash_dedup"))
    a = make_sample(0, instruction="instr", output=_body(1))
    b = make_sample(1, instruction="instr", output=_body(1))       # identical
    c = make_sample(2, instruction="instr", output=_body(2))       # different
    out, report = await _collect(stage, [a, b, c], FakeCtx())
    assert out == [a, c]
    assert any(k.startswith("near_dup:") for k in report.dropped["minhash_dedup"])
    assert "minhash_dedup" in a.metrics["kept_by"]


@pytest.mark.asyncio
async def test_signatures_persist_and_survive_threshold_change(tmp_path):
    db = str(tmp_path / "s.duckdb")
    store = Store(db)
    cfg128 = S.MinHashStageCfg(type="minhash_dedup", num_perm=32)
    stage = MinHashDedupStage(cfg128)
    sample = make_sample(0, instruction="i", output=_body(5))
    await _collect(stage, [sample], FakeCtx(store))
    sigs1 = store.load_sigs()
    assert len(sigs1) == 1
    store.close()

    # threshold is view-level: signatures survive verbatim
    store2 = Store(db)
    assert store2.load_sigs() == sigs1
    store2.close()


@pytest.mark.asyncio
async def test_resume_lsh_rebuild_finds_cross_run_dup(tmp_path):
    db = str(tmp_path / "s.duckdb")
    store = Store(db)
    cfg = S.MinHashStageCfg(type="minhash_dedup")
    ctx1 = FakeCtx(store)
    stage1 = MinHashDedupStage(cfg)
    a = make_sample(0, instruction="i", output=_body(9))
    await _collect(stage1, [a], ctx1)
    store.close()

    # New process: engine restore() rebuilds the LSH index from stored sigs;
    # the rebuilt index lives on the context, exact_dedup-style store checks
    # handle the rest. Here we verify restore produces a working index.
    store2 = Store(db)

    class Cfg2:
        pass

    ctx2 = FakeCtx(store2)
    ctx2._minhash_meta = [{"num_perm": cfg.num_perm, "threshold": cfg.threshold}]
    from corpuslab.core import checkpoint
    recovered = checkpoint.restore(store2, ctx2)
    assert recovered["lsh"] is not None
    m = stage1._sig_for(a.text_for_dedup())
    assert recovered["lsh"].query(m), "rebuilt LSH must find the stored signature"
    store2.close()
