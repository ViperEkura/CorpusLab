"""DuckDB sink (primary): renders each sample and commits it in one
transaction (projection + committed event + pending cleanup — atomic)."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from corpuslab.core.sample import RunReport, Sample
from corpuslab.sinks.renderers import render


class DuckDBSink:
    def __init__(self, fmt: str, thinking: bool = False, table: str = "samples"):
        self.fmt = fmt
        self.thinking = thinking
        self.table = table

    async def write(self, stream: AsyncIterator[Sample], ctx: Any) -> RunReport:
        assert ctx.store is not None
        report = ctx.report
        async for sample in stream:
            rendered = render(sample, self.fmt, thinking=self.thinking)
            total = float(sample.metrics.get("total_score") or 0.0)
            ctx.store.commit_sample(sample, rendered, total)
            report.produced += 1
        return report

    def export_jsonl(self, store: Any, path: str) -> int:
        """Post-run export of rendered rows (storage.export_format: jsonl)."""
        rows = store.read_rendered()
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return len(rows)
