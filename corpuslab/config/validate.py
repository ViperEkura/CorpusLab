"""Load-time validation (config-design.md §11): required-field sets differ per
subcommand."""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from corpuslab.config import schema as S
from corpuslab.core import planner

Level = str                                 # "error" | "warning"
Issue = Tuple[Level, str]


def check(cfg: S.Config, subcommand: str = "run",
          input_path: Optional[str] = None) -> List[Issue]:
    issues: List[Issue] = []

    def err(m):
        issues.append(("error", m))

    def warn(m):
        issues.append(("warning", m))

    # Endpoint requirements
    needs_llm = subcommand in ("run", "score")
    if needs_llm and not cfg.llm.model:
        err("llm.model is required (a model name is needed even with env vars)")

    # Endpoint references
    refs = []
    if cfg.judge.endpoint:
        refs.append(cfg.judge.endpoint)
    refs += [j.endpoint for j in cfg.judge.judges]
    for r in refs:
        if r != "llm" and r not in cfg.endpoints:
            err(f"endpoint reference {r!r} is not declared in endpoints")
    if cfg.judge.judges and cfg.judge.endpoint:
        warn("when judges is non-empty it takes precedence; judge.endpoint is ignored")

    # Per-subcommand required fields
    if subcommand == "run":
        if not cfg.strategies:
            err("run requires strategies (at least 1)")
        if cfg.output is None or not cfg.output.path:
            err("run requires output.path")
        # Yield (§10.3 boundaries)
        if cfg.strategies:
            try:
                planner.allocate(cfg.strategies, cfg.plan.count)
            except planner.PlanError as e:
                err(f"plan allocation failed: {e}")
        wsum = sum(max(s.weight, 0.0) for s in cfg.strategies)
        if cfg.strategies and abs(wsum - 1.0) > 1e-9:
            warn(f"strategies[].weight sums to {wsum:.3f}; it will be auto-normalized")
        for s in cfg.strategies:
            evo = getattr(s, "evolution", None)
            if evo and evo.crossover + evo.mutate > 1.0:
                warn(f"{s.type}: crossover+mutate={evo.crossover + evo.mutate:.2f} > 1; "
                     f"it will be auto-normalized")

    if subcommand == "score":
        if not cfg.judge.dimensions and not cfg.judge.scorers:
            err("score requires judge.dimensions (or non-empty scorers)")

    # Pipeline chain legality
    if not cfg.pipeline:
        warn("pipeline is empty: no governance will run")
    saw_batch = False
    for st in cfg.pipeline:
        if st.type in S.BATCH_STAGES:
            saw_batch = True
        elif saw_batch and st.type in S.STREAMING_STAGES:
            warn(f"streaming stage {st.type} after a batch stage: runnable but "
                 f"streaming benefits are lost")
    needs_embedding = any(s.type in ("semantic_dedup", "cluster_dedup")
                          for s in cfg.pipeline)
    if needs_embedding and not os.environ.get("CORPUSLAB_FAKE_EMBED") \
            and not (os.environ.get("EMBEDDING_BASE_URL") or cfg.embedding.base_url):
        warn("semantic_dedup/cluster_dedup need an embedding endpoint "
             "(embedding.base_url or $EMBEDDING_BASE_URL)")

    # Judge configuration sanity
    if (cfg.judge.judges or cfg.judge.endpoint) and not cfg.judge.dimensions:
        warn("judge.judges/endpoint declared but judge.dimensions is empty: "
             "remote judges will not run (only local scorers)")
    if cfg.judge.dimensions:
        smax = sum(d.max for d in cfg.judge.dimensions)
        if smax > 0 and cfg.judge.min_total > 0:
            ratio = cfg.judge.min_total / smax
            if ratio < 0.3 or ratio > 1.0:
                warn(f"min_total={cfg.judge.min_total} / Σmax={smax:.0f} ratio "
                     f"{ratio:.2f} outside 0.3–1.0; filtering may not behave as expected")
        dims = {d.name for d in cfg.judge.dimensions}
        for sc in cfg.judge.scorers:
            for dim in sc.dimensions:
                if dim not in dims:
                    warn(f"scorer dimension {dim!r} is not in judge.dimensions; "
                         f"it will not take effect")
        if cfg.judge.max_disagreement and not cfg.judge.judges:
            warn("max_disagreement only matters with multiple judges")

    # Resource existence
    def _exists(p: Optional[str], what: str):
        if p and not os.path.exists(p):
            err(f"{what} does not exist: {p}")

    for s in cfg.strategies:
        if getattr(s, "seed_file", None):
            _exists(s.seed_file, f"{s.type}.seed_file")
        if getattr(s, "document_file", None):
            _exists(s.document_file, f"{s.type}.document_file")
    for sc in cfg.judge.scorers:
        if sc.type == "fasttext" and sc.model_path:
            _exists(sc.model_path, "scorers[].model_path")
    if input_path:
        _exists(input_path, "input")

    # Output and storage
    if cfg.output is not None and cfg.output.storage.type == "jsonl":
        warn("storage.type=jsonl is the legacy plain-file mode: no resume "
             "checkpointing (the DuckDB mode is the full state store)")

    return issues


def has_errors(issues: List[Issue]) -> bool:
    return any(level == "error" for level, _ in issues)
