"""JSONL sink (legacy plain-file mode, storage.type=jsonl): no resume
checkpointing — the DuckDB mode is the full state store."""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

from corpuslab.core.sample import RunReport, Sample
from corpuslab.sinks.renderers import render


class JsonlSink:
    def __init__(self, fmt: str, thinking: bool = False):
        self.fmt = fmt
        self.thinking = thinking

    async def write(self, stream: AsyncIterator[Sample], ctx: Any) -> RunReport:
        report = ctx.report
        parent = os.path.dirname(os.path.abspath(ctx.cfg.output.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(ctx.cfg.output.path, "a", encoding="utf-8") as f:
            async for sample in stream:
                rendered = render(sample, self.fmt, thinking=self.thinking)
                f.write(json.dumps(rendered, ensure_ascii=False) + "\n")
                report.produced += 1
        return report
