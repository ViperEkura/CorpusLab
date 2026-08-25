"""Engine assembly + orchestration: builds the pipeline from config and runs
the four subcommand flows (run / clean / score / validate).

Test doubles are injected via env vars (CORPUSLAB_FAKE_LLM /
CORPUSLAB_FAKE_EMBED) so the CLI paths are e2e-testable offline."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Optional

from corpuslab.config import schema as S
from corpuslab.core import checkpoint, planner
from corpuslab.core.context import RunContext
from corpuslab.core.pipeline import Pipeline
from corpuslab.core.registry import lookup
from corpuslab.core.sample import Sample
from corpuslab.core.store import Store
from corpuslab.judges.aggregate import AggregateJudge
from corpuslab.judges.llm_judge import LLMJudge
from corpuslab.llm.client import CircuitBreakerOpen, HttpLLMClient
from corpuslab.sinks.duckdb_sink import DuckDBSink
from corpuslab.sinks.jsonl_sink import JsonlSink

log = logging.getLogger("corpuslab.engine")


def _make_llm(cfg: S.Config):
    if os.environ.get("CORPUSLAB_FAKE_LLM"):
        from corpuslab.testing import FakeLLM
        return FakeLLM()
    return HttpLLMClient(cfg)


def _make_embedding(cfg: S.Config, store, ctx):
    if os.environ.get("CORPUSLAB_FAKE_EMBED"):
        from corpuslab.testing import FakeEmbedding
        return FakeEmbedding()
    from corpuslab.config.loader import env_resolve_embedding
    return HttpEmbeddingClient(env_resolve_embedding(cfg), store=store, ctx=ctx)


def build_pipeline(cfg: S.Config) -> Pipeline:
    stages = []
    for st in cfg.pipeline:
        cls = lookup("stages", st.type)
        stages.append(cls(st))
    return Pipeline(stages)


def needs_embedding(cfg: S.Config) -> bool:
    emb_stages = {"semantic_dedup", "cluster_dedup"}
    if any(s.type in emb_stages for s in cfg.pipeline):
        return True
    return any(getattr(s, "chunking", None) is not None
               and s.chunking.enabled and s.chunking.mode == "semantic"
               for s in cfg.strategies)


def build_judge(cfg: S.Config) -> Optional[AggregateJudge]:
    has_remote = bool(cfg.judge.dimensions) and (
        cfg.judge.endpoint is not None or cfg.judge.judges or cfg.llm.model)
    scorers = []
    for sc in cfg.judge.scorers:
        if sc.type == "fasttext":
            from corpuslab.judges.local import FastTextScorer
            scorers.append(FastTextScorer(sc))
        else:
            raise ValueError(f"unknown scorer type: {sc.type}")
    if not has_remote and not scorers:
        return None
    remotes = []
    if cfg.judge.dimensions and (cfg.judge.judges or cfg.judge.endpoint is not None
                                 or cfg.llm.model):
        if cfg.judge.judges:
            for ref in cfg.judge.judges:
                remotes.append(LLMJudge(ref.endpoint, cfg.judge.dimensions,
                                        cfg.llm.lang))
        else:
            remotes.append(LLMJudge(cfg.judge.endpoint, cfg.judge.dimensions,
                                    cfg.llm.lang))
    return AggregateJudge(cfg.judge, remotes, scorers)


def _minhash_meta(cfg: S.Config) -> list:
    return [{"num_perm": s.num_perm, "threshold": s.threshold}
            for s in cfg.pipeline if s.type == "minhash_dedup"]


async def _strategy_streams(cfg: S.Config, ctx: RunContext,
                            allocations: list) -> AsyncIterator[Sample]:
    """Every strategy's execute feeds one merged stream (single global
    pipeline: cross-strategy dedup state is shared)."""
    async def _one(scfg, budget: int):
        strategy = lookup("strategies", scfg.type)(scfg)
        async def noop():
            if False:                          # pragma: no cover
                yield None
        specs = strategy.plan(noop(), budget, ctx)
        async for s in strategy.execute(specs, ctx):
            yield s
    gens = [_one(scfg, budget) for scfg, budget in allocations]
    queues = [asyncio.Queue(maxsize=64) for _ in gens]

    async def pump(i):
        try:
            async for item in gens[i]:
                await queues[i].put(item)
        finally:
            await queues[i].put(None)

    pumps = [asyncio.create_task(pump(i)) for i in range(len(gens))]
    finished = 0
    while finished < len(gens):
        for q in queues:
            if q.empty():
                continue
            item = q.get_nowait()
            if item is None:
                finished += 1
            else:
                yield item
            if finished >= len(gens):
                break
        else:
            await asyncio.sleep(0)
    for t in pumps:
        if not t.done():
            t.cancel()
    await asyncio.gather(*pumps, return_exceptions=True)


async def run_flow(cfg: S.Config, *, cli_count: Optional[int] = None,
                   only: Optional[list] = None, preview: bool = False,
                   resume: bool = False, discard_state: bool = False) -> Any:
    allocations = planner.allocate(cfg.strategies, cfg.plan.count,
                                   cli_count=cli_count, only=only)
    if preview:
        allocations = [(c, min(n, cfg.run.preview_count)) for c, n in allocations]

    from corpuslab.config.loader import effective_output_path
    path = effective_output_path(cfg)
    store = None if preview else Store(path, cfg.output.storage.table)
    ctx = RunContext(cfg, store=store, llm=_make_llm(cfg), preview=preview)
    ctx._minhash_meta = _minhash_meta(cfg)                  # noqa: SLF001
    if needs_embedding(cfg):
        ctx.embedding = _make_embedding(cfg, store, ctx)

    # ── resume reconciliation ─────────────────────────────
    if store is not None and resume:
        recovered = checkpoint.restore(store, ctx)
        ctx.terminal = recovered["terminal"]
        log.info("resume: %d terminal, %d planned, %d pending",
                 len(recovered["terminal"]), len(recovered["planned"]),
                 len(recovered["pending"]))

    if store is not None:
        try:
            checkpoint.write_manifest(store, cfg,
                                      num_perm=(ctx._minhash_meta[0]["num_perm"]
                                                if ctx._minhash_meta else None),
                                      embedding_model=(cfg.embedding.model
                                                       if needs_embedding(cfg) else None),
                                      discard=discard_state)
        except checkpoint.IncompatibleState:
            raise

    pipeline = build_pipeline(cfg)
    judge = build_judge(cfg)
    thinking = cfg.output.thinking
    from corpuslab.config.loader import derive_format
    fmt_out = derive_format(cfg)

    async def _scored() -> AsyncIterator[Sample]:
        async for s in pipeline.run(_strategy_streams(cfg, ctx, allocations), ctx):
            if judge is not None:
                sc = await judge.score(s, ctx)
                if sc is None:
                    continue                    # dropped by judge governance
            yield s

    if preview:
        # Preview: consume without side effects (no store, no files)
        async for _ in _scored():
            ctx.report.produced += 1
        return ctx.report

    if cfg.output.storage.type == "jsonl":
        sink = JsonlSink(fmt_out, thinking)
        report = await sink.write(_scored(), ctx)
    else:
        assert store is not None
        sink = DuckDBSink(fmt_out, thinking, cfg.output.storage.table)
        report = await sink.write(_scored(), ctx)
        export = cfg.output.storage.export_jsonl
        if export:
            sink.export_jsonl(store, export)

    if store is not None and cfg.output.cache_cleanup and not preview:
        store.cache_cleanup()
    if store is not None:
        store.close()
    return report


async def clean_flow(cfg: S.Config, input_path: str, output_path: Optional[str],
                     input_format: str = "flat", field_map: Optional[dict] = None,
                     resume: bool = False) -> Any:
    from corpuslab.config.loader import derive_format
    from corpuslab.sources.file import FileSource

    src = FileSource(input_path, input_format, field_map)
    out_cfg = output_path or (cfg.output.path if cfg.output else "./cleaned.duckdb")
    if out_cfg.endswith(".jsonl"):
        store = None
    else:
        if not out_cfg.endswith(".duckdb"):
            out_cfg += ".duckdb"
        store = Store(out_cfg, cfg.output.storage.table if cfg.output else "samples")
    ctx = RunContext(cfg, store=store, llm=None)
    pipeline = build_pipeline(cfg)
    fmt_out = derive_format(cfg)

    async def _files() -> AsyncIterator[Sample]:
        async for s in src.open(cfg, ctx):
            yield s

    if store is None:
        sink = JsonlSink(fmt_out, cfg.output.thinking if cfg.output else False)
        report = await sink.write(pipeline.run(_files(), ctx), ctx)
    else:
        sink = DuckDBSink(fmt_out, cfg.output.thinking if cfg.output else False,
                          cfg.output.storage.table if cfg.output else "samples")
        report = await sink.write(pipeline.run(_files(), ctx), ctx)
        store.close()
    return report


async def score_flow(cfg: S.Config, input_path: str, output_path: Optional[str],
                     input_format: str = "flat", field_map: Optional[dict] = None) -> Any:
    from corpuslab.config.loader import derive_format
    from corpuslab.sources.file import FileSource

    src = FileSource(input_path, input_format, field_map)
    out_cfg = output_path or (cfg.output.path if cfg.output else "./scored.duckdb")
    if out_cfg.endswith(".jsonl"):
        store = None
    else:
        if not out_cfg.endswith(".duckdb"):
            out_cfg += ".duckdb"
        store = Store(out_cfg, cfg.output.storage.table if cfg.output else "samples")
    ctx = RunContext(cfg, store=store, llm=_make_llm(cfg))
    judge = build_judge(cfg)
    if judge is None:
        raise ValueError("score requires judge dimensions or scorers")
    fmt_out = derive_format(cfg)

    async def _files() -> AsyncIterator[Sample]:
        async for s in src.open(cfg, ctx):
            yield s

    async def _scored() -> AsyncIterator[Sample]:
        async for s in _files():
            sc = await judge.score(s, ctx)
            if sc is None:
                continue
            yield s

    if store is None:
        sink = JsonlSink(fmt_out, cfg.output.thinking if cfg.output else False)
        report = await sink.write(_scored(), ctx)
    else:
        sink = DuckDBSink(fmt_out, cfg.output.thinking if cfg.output else False,
                          cfg.output.storage.table if cfg.output else "samples")
        report = await sink.write(_scored(), ctx)
        store.close()
    return report
