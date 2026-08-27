"""Pipeline tests: streaming chain, batch barrier with pending backpressure,
drop accounting, cross-strategy dedup via the shared store."""
from __future__ import annotations

import pytest

from corpuslab.core.context import RunContext
from corpuslab.core.pipeline import Pipeline
from corpuslab.core.registry import import_builtin_modules, lookup
from corpuslab.core.store import Store
from tests.conftest import make_sample

import_builtin_modules()


def build(pipeline_cfgs):
    from pydantic import TypeAdapter
    from corpuslab.config.schema import StageCfg
    ta = TypeAdapter(StageCfg)
    stages = []
    for c in pipeline_cfgs:
        cfg_obj = ta.validate_python(c)
        stages.append(lookup("stages", c["type"])(cfg_obj))
    return Pipeline(stages)


async def gen(samples):
    for s in samples:
        yield s


@pytest.mark.asyncio
async def test_streaming_chain_and_drops(tmp_path):
    store = Store(str(tmp_path / "s.duckdb"))
    ctx = RunContext(_min_cfg(tmp_path), store=store)
    pipeline = build([
        {"type": "length", "instruction": [5, 4000], "output": [10, 8000]},
        {"type": "stats", "min_ngram_diversity": 0.2},
    ])
    good = make_sample(1)
    too_short = make_sample(2, output="short")
    out = [s async for s in pipeline.run(gen([good, too_short]), ctx)]
    assert out == [good]
    assert too_short.id in store.dropped_ids()
    store.close()


def _min_cfg(tmp_path):
    from tests.conftest import make_config
    return make_config(tmp_path)


@pytest.mark.asyncio
async def test_batch_barrier_pends_and_clears(tmp_path):
    store = Store(str(tmp_path / "s.duckdb"))
    ctx = RunContext(_min_cfg(tmp_path), store=store)
    pipeline = build([
        {"type": "exact_dedup"},
        {"type": "semantic_dedup", "threshold": 0.85},
    ])
    samples = [make_sample(i) for i in range(3)]

    # Start consuming; while inside the barrier all samples are in pending
    async def collect():
        return [s async for s in pipeline.run(gen(samples), ctx)]

    # Need an embedding client for the batch stage
    from corpuslab.testing import FakeEmbedding
    ctx.embedding = FakeEmbedding()
    out = await collect()
    assert len(out) == 3                          # all distinct → all kept
    # After the barrier: committed (via store) or dropped; nothing pending
    for s in out:
        store.commit_sample(s, {"x": 1})
    assert store.conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    store.close()


@pytest.mark.asyncio
async def test_cross_strategy_dedup_shares_state(tmp_path):
    """Two consecutive pipeline runs (simulating two strategies feeding one
    global pipeline): a duplicate across runs must be caught via the store."""
    store = Store(str(tmp_path / "s.duckdb"))
    ctx = RunContext(_min_cfg(tmp_path), store=store)
    from corpuslab.testing import FakeEmbedding
    ctx.embedding = FakeEmbedding()

    pipeline_a = build([{"type": "exact_dedup"}])
    pipeline_b = build([{"type": "exact_dedup"}])     # fresh in-memory state

    dup = make_sample(99)
    a = [make_sample(1), dup]
    b = [make_sample(2), dup]                         # cross-run duplicate

    out_a = [s async for s in pipeline_a.run(gen(a), ctx)]
    out_b = [s async for s in pipeline_b.run(gen(b), ctx)]
    assert len(out_a) == 2
    assert len(out_b) == 1                            # dup caught via store
    reasons = store.conn.execute(
        "SELECT reason FROM dropped").fetchall()
    assert any("duplicate" in r[0] for r in reasons)
    store.close()


@pytest.mark.asyncio
async def test_empty_pipeline_passthrough(tmp_path):
    ctx = RunContext(_min_cfg(tmp_path), store=None)
    pipeline = Pipeline([])
    samples = [make_sample(i) for i in range(2)]
    out = [s async for s in pipeline.run(gen(samples), ctx)]
    assert out == samples
