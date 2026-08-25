"""Material loading: topics / seeds / documents / tools / file (S5: read side
effects live only in sources)."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional


@dataclass
class SimpleMaterial:
    kind: str
    payload: dict = field(default_factory=dict)


# ── topics ────────────────────────────────────────────────
def load_topics(strategy_cfg: Any) -> List[SimpleMaterial]:
    total = sum(max(t.weight, 0.0) for t in strategy_cfg.topics) or 1.0
    out = []
    for t in strategy_cfg.topics:
        p = {"topic": t.topic, "weight": max(t.weight, 0.0) / total}
        if t.knowledge:
            p["knowledge"] = t.knowledge
        out.append(SimpleMaterial("topic", p))
    return out


# ── seeds ─────────────────────────────────────────────────
def load_seeds(path: str, field_map: Optional[Dict[str, str]] = None
               ) -> List[SimpleMaterial]:
    """Seed JSONL + field_map adaptation (foreign fields → canonical fields)."""
    field_map = field_map or {}
    seeds = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            d = {field_map.get(k, k): v for k, v in d.items()}
            if "instruction" not in d and "prompt" in d:
                d["instruction"] = d.pop("prompt")
            if "output" not in d and "response" in d:
                d["output"] = d.pop("response")
            d.setdefault("id", f"seed:{os.path.basename(path)}:{ln}")
            seeds.append(SimpleMaterial("seed", d))
    if not seeds:
        raise ValueError(f"seed file is empty: {path}")
    return seeds


# ── documents ─────────────────────────────────────────────
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))


def normalize_text(text: str) -> str:
    """Unicode normalization: strip BOM/zero-width/control characters while
    keeping Markdown and CJK punctuation."""
    text = text.translate(_ZERO_WIDTH)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t"
                   or unicodedata.category(ch) not in ("Cc", "Cf"))
    return text.strip()


def load_documents(path: str, field_map: Optional[Dict[str, str]] = None
                   ) -> List[SimpleMaterial]:
    field_map = field_map or {}
    docs: List[SimpleMaterial] = []
    base = os.path.basename(path)

    if path.endswith((".json", ".jsonl")):
        with open(path, "r", encoding="utf-8") as f:
            if path.endswith(".jsonl"):
                rows = [json.loads(ln) for ln in f if ln.strip()]
            else:
                rows = json.load(f)
                rows = rows if isinstance(rows, list) else [rows]
        for i, row in enumerate(rows):
            row = {field_map.get(k, k): v for k, v in row.items()}
            text = row.get("text") or row.get("content") or ""
            docs.append(SimpleMaterial("document", {
                "id": row.get("id") or f"{base}:{i}",
                "text": normalize_text(str(text)),
                "meta": {k: v for k, v in row.items()
                         if k not in ("text", "content", "id")},
            }))
    else:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        docs.append(SimpleMaterial("document", {
            "id": base, "text": normalize_text(text), "meta": {}}))
    docs = [d for d in docs if d.payload.get("text")]
    if not docs:
        raise ValueError(f"no usable content in document: {path}")
    return docs


# ── chunking ──────────────────────────────────────────────
def structure_chunks(text: str, min_len: int, max_len: int) -> List[tuple]:
    """Structure chunking: aggregate by paragraph, split when exceeding
    max_len. Returns [(start, end)] character ranges."""
    paras: List[tuple] = []
    pos = 0
    for para in re.split(r"\n\s*\n", text):
        plen = len(para)
        if plen == 0:
            pos += 1
            continue
        start, end = pos, pos + plen
        if plen > max_len:
            for i in range(start, end, max_len):
                paras.append((i, min(i + max_len, end)))
        else:
            paras.append((start, end))
        pos = end + 1
    # Merge too-short paragraphs forward
    merged: List[tuple] = []
    for rng in paras:
        if merged and rng[1] - rng[0] < min_len:
            prev = merged[-1]
            merged[-1] = (prev[0], rng[1])
        else:
            merged.append(rng)
    return [r for r in merged if r[1] - r[0] >= min(30, min_len)]


async def semantic_chunks(text: str, cfg: Any, ctx: Any) -> List[tuple]:
    """Semantic chunking: cut where adjacent sentence-vector cosine drops
    below the threshold (consumes the global embedding client)."""
    import math

    sentences = [s for s in re.split(r"(?<=[。！？.!?\n])", text) if s.strip()]
    if len(sentences) <= 1:
        return [(0, len(text))]
    vecs = await ctx.embed(sentences)
    bounds = [0]
    starts = []
    pos = 0
    for s in sentences:
        starts.append(pos)
        pos += len(s)
    for i in range(1, len(sentences)):
        a, b = vecs[i - 1], vecs[i]
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1e-9
        nb = math.sqrt(sum(x * x for x in b)) or 1e-9
        cos = dot / (na * nb)
        end = starts[i]
        last = bounds[-1]
        if cos < cfg.similarity_threshold or end - last >= cfg.max_chunk_length:
            bounds.append(end)
    bounds.append(len(text))
    chunks = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)
              if bounds[i + 1] > bounds[i]]
    return chunks or [(0, len(text))]


# ── tools ─────────────────────────────────────────────────
def load_tools(tools: List[dict]) -> List[SimpleMaterial]:
    """Validate OpenAI function tools: type=function with function.name and
    parameters present."""
    out = []
    for i, t in enumerate(tools):
        fn = t.get("function") if isinstance(t, dict) else None
        if not fn or not fn.get("name") or "parameters" not in fn:
            raise ValueError(f"tools[{i}] is not a valid OpenAI function definition")
        out.append(SimpleMaterial("tool", {"tool": t, "fn": fn}))
    if not out:
        raise ValueError("tools is empty")
    return out


# ── file (entry point for clean/score) ───────────────────
def iter_input_rows(path: str) -> List[dict]:
    """Foreign input → dict rows (JSONL, JSON, or the rendered/canonical rows
    of a DuckDB state store)."""
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
