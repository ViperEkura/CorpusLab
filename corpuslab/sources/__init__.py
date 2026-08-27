"""sources package: raw material reading (S5: read side effects live only
here). Each module provides one loader plus its Source protocol adapter."""
from __future__ import annotations

__all__ = [
    "SimpleMaterial",
    "load_topics",
    "TopicSource",
    "load_seeds",
    "SeedSource",
    "load_documents",
    "normalize_text",
    "DocumentSource",
    "structure_chunks",
    "semantic_chunks",
    "load_tools",
    "ToolSource",
    "iter_input_rows",
    "materialize",
]

import json
from typing import Any, AsyncIterator, Optional

from corpuslab.sources.topics import SimpleMaterial, load_topics
from corpuslab.sources.seeds import load_seeds
from corpuslab.sources.documents import load_documents, normalize_text
from corpuslab.sources.chunking import semantic_chunks, structure_chunks
from corpuslab.sources.tools import load_tools


# Source adapters: bridge the sync loaders to the async Material stream the
# engine feeds into strategy.plan() (project-structure.md §5.1 control flow).


class TopicSource:
    kind = "topic"

    def __init__(self, strategy_cfg: Any):
        self.strategy_cfg = strategy_cfg

    async def open(self, cfg: Any, ctx: Any):
        for m in load_topics(self.strategy_cfg):
            yield m


class SeedSource:
    kind = "seed"

    def __init__(self, seed_file: str, field_map: Optional[dict] = None):
        self.seed_file = seed_file
        self.field_map = field_map or {}

    async def open(self, cfg: Any, ctx: Any):
        for m in load_seeds(self.seed_file, self.field_map):
            yield m


class DocumentSource:
    kind = "document"

    def __init__(self, document_file: str, field_map: Optional[dict] = None):
        self.document_file = document_file
        self.field_map = field_map or {}

    async def open(self, cfg: Any, ctx: Any):
        for m in load_documents(self.document_file, self.field_map):
            yield m


class ToolSource:
    kind = "tool"

    def __init__(self, tools: list):
        self.tools = tools

    async def open(self, cfg: Any, ctx: Any):
        for m in load_tools(self.tools):
            yield m


def iter_input_rows(path: str) -> list:
    """Foreign input → dict rows (JSONL, JSON, or the rendered/canonical rows
    of a DuckDB state store) — entry point for clean/score."""
    if path.endswith(".duckdb"):
        import duckdb
        con = duckdb.connect(path, read_only=True)
        try:
            tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
            table = "samples" if "samples" in tables else tables[0]
            rows = con.execute(
                f"SELECT rendered FROM {table} WHERE rendered IS NOT NULL "
                f"UNION ALL "
                f"SELECT payload FROM {table} WHERE rendered IS NULL"
            ).fetchall()
            return [json.loads(r[0]) for r in rows]
        finally:
            con.close()
    with open(path, "r", encoding="utf-8") as f:          # .jsonl / .json
        if path.endswith(".json"):
            data = json.load(f)
            return data if isinstance(data, list) else [data]
        return [json.loads(ln) for ln in f if ln.strip()]


async def materialize(materials) -> AsyncIterator[Any]:
    for m in materials:
        yield m
