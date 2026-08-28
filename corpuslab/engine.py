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
from corpuslab.config.loader import (derive_format, env_resolve_embedding,
                                     layout_for_path, output_layout)
from corpuslab.llm.endpoints import env_fallback_llm, merge_endpoint
from corpuslab.core import checkpoint, planner
from corpuslab.core.context import RunContext
from corpuslab.core.pipeline import Pipeline
from corpuslab.core.registry import lookup
from corpuslab.core.sample import Sample
from corpuslab.core.store import Store
from corpuslab.embedding.client import HttpEmbeddingClient
from corpuslab.judges.aggregate import AggregateJudge
from corpuslab.judges.llm_judge import LLMJudge
from corpuslab.judges.local import FastTextScorer
from corpuslab.judges.perplexity import PerplexityScorer
from corpuslab.llm.client import HttpLLMClient
from corpuslab.sinks.duckdb_sink import DuckDBSink
from corpuslab.sinks.jsonl_sink import JsonlSink
from corpuslab.sources.file import FileSource

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
            scorers.append(FastTextScorer(sc))
        else:
            raise ValueError(f"unknown scorer type: {sc.type}")
    if cfg.judge.perplexity is not None:
        pcfg = cfg.judge.perplexity
        base = env_fallback_llm(merge_endpoint(cfg, None))
        resolved = {
            "model": pcfg.model or base.model,
            "base_url": (pcfg.base_url or base.base_url or "").rstrip("/"),
            "api_key": pcfg.api_key or base.api_key,
        }
        scorers.append(PerplexityScorer(pcfg,
                                        model=resolved["model"],
                                        base_url=resolved["base_url"],
                                        api_key=resolved["api_key"]))
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


def _source_for(scfg: Any):
    """The declared material source for a strategy (§3.3). Concept sources
    read topics; sample sources read seeds; document sources read documents;
    spec sources read tools."""
    from corpuslab.sources import (DocumentSource, SeedSource, TopicSource,
                                   ToolSource)
    t = scfg.type
    if t in ("topic_driven", "deep_thinking"):
        return TopicSource(scfg)
    if t in ("seed_driven", "evol_instruct"):
        return SeedSource(scfg.seed_file, scfg.field_map)
    if t == "document_qa":
        return DocumentSource(scfg.document_file, scfg.field_map)
    if t == "instruction_backtranslation":
        return DocumentSource(scfg.document_file, scfg.field_map)
    if t == "tool_call":
        return ToolSource(scfg.tools)
    raise ValueError(f"no known material source for strategy {t!r}")


async def _strategy_streams(cfg: S.Config, ctx: RunContext,
                            allocations: list) -> AsyncIterator[Sample]:
    """Every strategy's execute feeds one merged stream (single global
    pipeline: cross-strategy dedup state is shared). Control flow follows
    project-structure.md §5.1: source.open → strategy.plan → execute."""
    async def _one(scfg, budget: int):
        src = _source_for(scfg)
        strategy = lookup("strategies", scfg.type)(scfg)
        specs = strategy.plan(src.open(cfg, ctx), budget, ctx)
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
        # Preview keeps the total at preview_count: scale every share so the
        # sum matches run.preview_count (not N x preview_count).
        total_alloc = sum(n for _, n in allocations)
        pc = cfg.run.preview_count
        scaled = []
        for c, n in allocations:
            k = round(n * pc / max(total_alloc, 1)) if total_alloc else 0
            scaled.append((c, max(min(n, 1), k)))
        rest = pc - sum(k for _, k in scaled)
        if rest and scaled:
            i = max(range(len(scaled)), key=lambda j: scaled[j][1])
            scaled[i] = (scaled[i][0], scaled[i][1] + rest)
        allocations = scaled

    layout = output_layout(cfg)
    store = None if preview else Store(layout["db_path"], cfg.output.storage.table)
    ctx = RunContext(cfg, store=store, llm=_make_llm(cfg), preview=preview)
    ctx._minhash_meta = _minhash_meta(cfg)                  # noqa: SLF001
    if needs_embedding(cfg):
        ctx.embedding = _make_embedding(cfg, store, ctx)

    # resume reconciliation
    if store is not None and resume:
        recovered = checkpoint.restore(store, ctx)
        ctx.terminal = recovered["terminal"]
        log.info("resume: %d terminal, %d planned, %d pending",
                 len(recovered["terminal"]), len(recovered["planned"]),
                 len(recovered["pending"]))

    if store is not None:
        # Manifest compatibility gate (checkpoint-design.md §7): refuses on
        # version/config/seed drift unless --discard-state; drops only the
        # affected state (sigs/embeddings) for parameter drift.
        checkpoint.write_manifest(store, cfg,
                                  num_perm=(ctx._minhash_meta[0]["num_perm"]
                                            if ctx._minhash_meta else None),
                                  embedding_model=(cfg.embedding.model
                                                   if needs_embedding(cfg) else None),
                                  discard=discard_state)

    pipeline = build_pipeline(cfg)
    judge = build_judge(cfg)
    thinking = cfg.output.thinking
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
        # Export: one projection file per storage.export_format
        export_path = layout["export_path"]
        if export_path:
            if export_path.endswith(".parquet"):
                n = store.export_parquet(export_path)
                log.info("parquet export: %d rows → %s", n, export_path)
            else:
                n = sink.export_jsonl(store, export_path)
                log.info("jsonl export: %d rows → %s", n, export_path)

    if store is not None and cfg.output.cache_cleanup and not preview:
        store.cache_cleanup()
    if store is not None:
        store.close()
    return report


async def clean_flow(cfg: S.Config, input_path: str, output_path: Optional[str],
                     input_format: str = "flat", field_map: Optional[dict] = None,
                     resume: bool = False) -> Any:
    src = FileSource(input_path, input_format, field_map)
    storage = cfg.output.storage if cfg.output else None
    out_cfg = output_path or (cfg.output.path if cfg.output else "./cleaned")
    if storage is None:
        storage = S.StorageCfg()
    layout = layout_for_path(out_cfg, storage)
    store = None if layout["dir_mode"] is False and out_cfg.endswith(".jsonl") \
        else Store(layout["db_path"], storage.table)
    ctx = RunContext(cfg, store=store, llm=None)
    pipeline = build_pipeline(cfg)
    fmt_out = derive_format(cfg)
    thinking = cfg.output.thinking if cfg.output else False

    async def _files() -> AsyncIterator[Sample]:
        async for s in src.open(cfg, ctx):
            yield s

    if store is None:
        sink = JsonlSink(fmt_out, thinking)
        report = await sink.write(pipeline.run(_files(), ctx), ctx)
    else:
        sink = DuckDBSink(fmt_out, thinking, storage.table)
        report = await sink.write(pipeline.run(_files(), ctx), ctx)
        export_path = layout["export_path"]
        if export_path:
            if export_path.endswith(".parquet"):
                store.export_parquet(export_path)
            else:
                sink.export_jsonl(store, export_path)
        store.close()
    return report


async def score_flow(cfg: S.Config, input_path: str, output_path: Optional[str],
                     input_format: str = "flat", field_map: Optional[dict] = None) -> Any:
    src = FileSource(input_path, input_format, field_map)
    storage = cfg.output.storage if cfg.output else None
    out_cfg = output_path or (cfg.output.path if cfg.output else "./scored")
    if storage is None:
        storage = S.StorageCfg()
    layout = layout_for_path(out_cfg, storage)
    store = None if layout["dir_mode"] is False and out_cfg.endswith(".jsonl") \
        else Store(layout["db_path"], storage.table)
    ctx = RunContext(cfg, store=store, llm=_make_llm(cfg))
    judge = build_judge(cfg)
    if judge is None:
        raise ValueError("score requires judge dimensions or scorers")
    fmt_out = derive_format(cfg)
    thinking = cfg.output.thinking if cfg.output else False

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
        sink = JsonlSink(fmt_out, thinking)
        report = await sink.write(_scored(), ctx)
    else:
        sink = DuckDBSink(fmt_out, thinking, storage.table)
        report = await sink.write(_scored(), ctx)
        export_path = layout["export_path"]
        if export_path:
            if export_path.endswith(".parquet"):
                store.export_parquet(export_path)
            else:
                sink.export_jsonl(store, export_path)
        store.close()
    return report
