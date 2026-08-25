"""The single global embedding client: batching + content-addressed cache in
store.embeddings.

Same retry semantics as llm; credentials never fall back to llm (prevents
sending keys to a third-party endpoint, config-design §10.4).
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, List, Optional

import httpx

from corpuslab.llm.client import retry_with_backoff

log = logging.getLogger("corpuslab.embedding")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HttpEmbeddingClient:
    def __init__(self, cfg: Any, store: Any = None, ctx: Any = None):
        self.model = cfg.model
        self.base_url = cfg.base_url or os.environ.get("EMBEDDING_BASE_URL")
        self.api_key = cfg.api_key or os.environ.get("EMBEDDING_API_KEY")
        self.batch_size = cfg.batch_size
        self.store = store
        self.ctx = ctx

    async def embed(self, texts: List[str]) -> List[List[float]]:
        out: List[Optional[List[float]]] = [None] * len(texts)
        hashes = [text_hash(t) for t in texts]

        # Content-addressed cache hits
        if self.store is not None:
            cached = self.store.load_embeddings(hashes, self.model)
            for i, h in enumerate(hashes):
                if h in cached:
                    out[i] = list(cached[h])
                    if self.ctx is not None:
                        self.ctx.report.cache_hits += 1

        missing = [i for i, v in enumerate(out) if v is None]
        for i in range(0, len(missing), self.batch_size):
            batch_idx = missing[i:i + self.batch_size]
            vecs = await self._embed_batch([texts[j] for j in batch_idx])
            if self.ctx is not None:
                self.ctx.report.embed_calls += len(batch_idx)
            fresh = []
            for j, v in zip(batch_idx, vecs):
                out[j] = v
                fresh.append((hashes[j], self.model, v))
            if self.store is not None:
                self.store.save_embeddings(fresh)
        return [v for v in out]

    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        if not self.base_url:
            raise RuntimeError(
                "embedding endpoint not configured (embedding.base_url or "
                "$EMBEDDING_BASE_URL)")

        async def _once():
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    self.base_url.rstrip("/") + "/embeddings",
                    json={"model": self.model, "input": texts}, headers=headers)
                r.raise_for_status()
                data = r.json()
            order = {d["index"]: d["embedding"] for d in data["data"]}
            return [order[i] for i in range(len(texts))]

        return await retry_with_backoff(_once, attempts=3, backoff=2.0, max_delay=30)
