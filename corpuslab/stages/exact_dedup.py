"""exact_dedup stage (streaming): SHA256 dedup; state lives in the
fingerprints table."""
from __future__ import annotations

from typing import Any, AsyncIterator

from corpuslab.core.registry import register_stage
from corpuslab.core.sample import Sample


@register_stage("exact_dedup", scheduling="streaming")
class ExactDedupStage:
    type = "exact_dedup"

    def __init__(self, cfg: Any = None):          # noqa: ARG002 (no parameters)
        self._mem: set = set()                    # in-memory fast path

    async def apply_stream(self, stream: AsyncIterator[Sample],
                           ctx: Any) -> AsyncIterator[Sample]:
        async for s in stream:
            fp = s.fingerprint()
            if fp in self._mem:
                ctx.drop(s, "exact_dedup", "duplicate")
                continue
            # Resume path: fingerprints recorded by earlier runs live in the
            # store (a dropped sample's fingerprint must survive — otherwise
            # its duplicates slip through on the next run)
            if ctx.store is not None and not ctx.preview \
                    and ctx.store.has_fingerprint(fp):
                self._mem.add(fp)
                ctx.drop(s, "exact_dedup", "duplicate(resume)")
                continue
            self._mem.add(fp)
            if ctx.store is not None and not ctx.preview:
                ctx.store.add_fingerprint(fp, s.id)
            s.kept_by("exact_dedup")
            yield s
