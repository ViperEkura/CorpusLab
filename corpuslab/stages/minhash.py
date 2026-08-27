"""minhash_dedup stage (streaming): MinHash LSH near-dup removal.

Signatures are persisted (state); the LSH index is rebuilt from them on
resume (a materialized view). Changing `threshold` stays resume-compatible —
only `num_perm` invalidates signatures (manifest-enforced)."""
from __future__ import annotations

from typing import Any, AsyncIterator

from datasketch import MinHash, MinHashLSH

from corpuslab.core.registry import register_stage
from corpuslab.core.sample import Sample


def _ngrams(text: str, n: int):
    text = " ".join(text.split())
    return [text[i:i + n] for i in range(max(len(text) - n + 1, 0))]


@register_stage("minhash_dedup", scheduling="streaming")
class MinHashDedupStage:
    type = "minhash_dedup"

    def __init__(self, cfg: Any):
        self.threshold = cfg.threshold
        self.num_perm = cfg.num_perm
        self.ngram_n = cfg.ngram_n
        self.lsh = MinHashLSH(threshold=cfg.threshold, num_perm=cfg.num_perm)

    def _sig_for(self, text: str):
        m = MinHash(num_perm=self.num_perm)
        for g in _ngrams(text, self.ngram_n):
            m.update(g.encode("utf-8"))
        return m

    async def apply_stream(self, stream: AsyncIterator[Sample],
                           ctx: Any) -> AsyncIterator[Sample]:
        async for s in stream:
            m = self._sig_for(s.text_for_dedup())
            candidates = self.lsh.query(m)
            if candidates:
                ctx.drop(s, "minhash_dedup",
                         f"near_dup:{candidates[0]}")
                continue
            try:
                self.lsh.insert(s.id, m)
            except ValueError:
                pass                        # duplicate insert (idempotent replay)
            if ctx.store is not None and not ctx.preview:
                ctx.store.save_sig(s.id, [int(x) for x in m.hashvalues])
            s.kept_by("minhash_dedup")
            yield s
