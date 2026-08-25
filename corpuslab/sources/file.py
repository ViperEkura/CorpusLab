"""FileSource: foreign JSONL/DuckDB → canonical Sample reverse adaptation
(entry point for clean/score).

--input-format selects alpaca/chatml/sharegpt/openai/flat;
--field-map renames fields. The renderers' reverse parsing is reused here
(S5 pure functions).
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

from corpuslab.core.registry import register_source
from corpuslab.core.sample import Sample
from corpuslab.sources import iter_input_rows


def _id_for(row: dict, fmt: str, idx: int) -> str:
    if row.get("id"):
        return str(row["id"])
    if "metadata" in row and isinstance(row["metadata"], dict) and row["metadata"].get("id"):
        return str(row["metadata"]["id"])
    key = repr(sorted(row.items()))
    return f"file:{fmt}:{hashlib.sha256(key.encode()).hexdigest()[:16]}:{idx}"


def row_to_sample(row: dict, fmt: str, field_map: Optional[dict] = None,
                  idx: int = 0) -> Sample:
    field_map = field_map or {}
    row = {field_map.get(k, k): v for k, v in row.items()}
    sid = _id_for(row, fmt, idx)
    strategy = "file"

    if fmt == "chatml":
        msgs = row.get("messages") or []
        return Sample(id=sid, strategy=strategy, messages=msgs)
    if fmt == "openai":
        msgs = row.get("messages") or []
        return Sample(id=sid, strategy=strategy, messages=msgs,
                      tools=row.get("tools") or [])
    if fmt == "sharegpt":
        convs = row.get("conversations") or []
        role_map = {"human": "user", "gpt": "assistant", "system": "system",
                    "user": "user", "assistant": "assistant"}
        msgs = [{"role": role_map.get(c.get("from", ""), "user"),
                 "content": c.get("value", "")} for c in convs]
        return Sample(id=sid, strategy=strategy, messages=msgs)
    # flat: already a canonical single sample (clean/score over the internal store)
    if "messages" in row:
        return Sample(id=sid, strategy=strategy, messages=row.get("messages") or [],
                      tools=row.get("tools") or [],
                      metadata={"lineage": {"input": "file"}})
    # alpaca (default)
    md = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    md.setdefault("lineage", {"input": "file"})
    return Sample(
        id=sid, strategy=strategy,
        instruction=str(row.get("instruction") or row.get("query") or ""),
        output=str(row.get("output") or row.get("response") or ""),
        reasoning=str(row.get("reasoning") or ""),
        metadata=md,
    )


@register_source("file")
class FileSource:
    kind = "file"

    def __init__(self, path: str, input_format: str = "flat",
                 field_map: Optional[dict] = None):
        self.path = path
        self.input_format = input_format
        self.field_map = field_map or {}

    async def open(self, cfg: Any, ctx: Any):
        rows = iter_input_rows(self.path)
        for i, row in enumerate(rows):
            sample = row_to_sample(row, self.input_format, self.field_map, i)
            if not sample.core_text().strip():
                continue
            sample.validate()          # internal-format contract (data-format.md §2)
            yield sample
