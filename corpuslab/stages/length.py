"""length stage (streaming): instruction/output length gate."""
from __future__ import annotations

from typing import Any, AsyncIterator

from corpuslab.core.registry import register_stage
from corpuslab.core.sample import Sample


@register_stage("length", scheduling="streaming")
class LengthStage:
    type = "length"

    def __init__(self, cfg: Any):
        self.i_range = tuple(cfg.instruction)
        self.o_range = tuple(cfg.output)

    def _text_of(self, sample: Sample, part: str) -> int:
        if sample.messages:
            contents = [m.get("content") or "" for m in sample.messages]
            if part == "instruction":
                # first non-system message is the query side
                for m in sample.messages:
                    if m.get("role") in ("user", "human"):
                        return len(m.get("content") or "")
                return 0
            return sum(len(c) for c in contents)
        text = sample.instruction if part == "instruction" else sample.output
        return len(text or "")

    async def apply_stream(self, stream: AsyncIterator[Sample],
                           ctx: Any) -> AsyncIterator[Sample]:
        async for s in stream:
            ilen = self._text_of(s, "instruction")
            olen = self._text_of(s, "output")
            if not (self.i_range[0] <= ilen <= self.i_range[1]):
                ctx.drop(s, "length", f"instruction_len:{ilen}")
                continue
            if not (self.o_range[0] <= olen <= self.o_range[1]):
                ctx.drop(s, "length", f"output_len:{olen}")
                continue
            s.kept_by("length")
            yield s
