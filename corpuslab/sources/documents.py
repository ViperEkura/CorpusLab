"""documents: .md/.txt/.json/.jsonl loading + Unicode normalization."""
from __future__ import annotations

__all__ = ["normalize_text", "load_documents"]

import json
import os
import unicodedata
from typing import Dict, Optional

from corpuslab.sources.topics import SimpleMaterial

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))


def normalize_text(text: str) -> str:
    """Unicode normalization: strip BOM/zero-width/control characters while
    keeping Markdown and CJK punctuation."""
    text = text.translate(_ZERO_WIDTH)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t"
                   or unicodedata.category(ch) not in ("Cc", "Cf"))
    return text.strip()


def load_documents(path: str, field_map: Optional[Dict[str, str]] = None) -> list:
    field_map = field_map or {}
    docs = []
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
