"""Batch dedup stages: semantic_dedup / cluster_dedup.

Batch stages are async — they call the embedding client. In-flight samples
reach the barrier via the pending table (disk backpressure, handled by the
Pipeline engine), and embeddings are served from the content-addressed cache
in the store."""
from __future__ import annotations

import math
import random
from typing import Any, List

from corpuslab.core.registry import register_stage
from corpuslab.core.sample import Sample


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


@register_stage("semantic_dedup", scheduling="batch")
class SemanticDedupStage:
    type = "semantic_dedup"

    def __init__(self, cfg: Any):
        self.threshold = cfg.threshold

    async def apply_batch(self, samples: List[Sample], ctx: Any) -> List[Sample]:
        if len(samples) <= 1:
            return samples
        texts = [s.text_for_dedup() for s in samples]
        vecs = await ctx.embed(texts)
        kept: List[Sample] = []
        kept_vecs: List[list] = []
        for s, v in zip(samples, vecs):
            dup_of = None
            for k, kv in zip(kept, kept_vecs):
                if _cosine(v, kv) >= self.threshold:
                    dup_of = k.id
                    break
            if dup_of is not None:
                ctx.drop(s, "semantic_dedup", f"cosine_dup:{dup_of}")
                continue
            kept.append(s)
            kept_vecs.append(v)
            s.kept_by("semantic_dedup")
        return kept


@register_stage("cluster_dedup", scheduling="batch")
class ClusterDedupStage:
    type = "cluster_dedup"

    def __init__(self, cfg: Any = None):          # noqa: ARG002 (no parameters)
        pass

    async def apply_batch(self, samples: List[Sample], ctx: Any) -> List[Sample]:
        """LSH-style bucketing over embeddings, then per-cluster semantic
        pruning (keep the first of each tight cluster)."""
        if len(samples) <= 1:
            return samples
        vecs = await ctx.embed([s.text_for_dedup() for s in samples])

        # Greedy bucketing: bucket key = sign pattern of a few random projections
        rng = random.Random(0)
        dims = len(vecs[0]) if vecs else 0
        k = min(8, max(dims, 1))
        proj = [[rng.uniform(-1, 1) for _ in range(dims)] for _ in range(k)]

        def bucket(v) -> tuple:
            return tuple(0 if sum(x * w for x, w in zip(v, p)) >= 0 else 1
                         for p in proj)

        clusters: dict = {}
        for s, v in zip(samples, vecs):
            clusters.setdefault(bucket(v), []).append((s, v))

        kept: List[Sample] = []
        for members in clusters.values():
            if len(members) == 1:
                kept.append(members[0][0])
                continue
            # Inside a crowded cluster, keep the first and drop near-dups
            anchor_s, anchor_v = members[0]
            anchor_s.kept_by("cluster_dedup")
            kept.append(anchor_s)
            for s, v in members[1:]:
                if _cosine(v, anchor_v) >= 0.9:
                    ctx.drop(s, "cluster_dedup", f"crowded_cluster:{anchor_s.id}")
                else:
                    s.kept_by("cluster_dedup")
                    kept.append(s)
        return kept
