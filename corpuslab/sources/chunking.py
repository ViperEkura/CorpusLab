"""chunking: structure chunking / semantic chunking (consumes the embedding
client via ctx.embed)."""
from __future__ import annotations

__all__ = ["structure_chunks", "semantic_chunks"]

import math
import re
from typing import Any, List


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
