"""seeds: seed JSONL + field_map adaptation (foreign fields → canonical)."""
from __future__ import annotations

__all__ = ["load_seeds"]

import json
import os
from typing import Dict, Optional

from corpuslab.sources.topics import SimpleMaterial


def load_seeds(path: str, field_map: Optional[Dict[str, str]] = None) -> list:
    field_map = field_map or {}
    seeds = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            d = {field_map.get(k, k): v for k, v in d.items()}
            if "instruction" not in d and "prompt" in d:
                d["instruction"] = d.pop("prompt")
            if "output" not in d and "response" in d:
                d["output"] = d.pop("response")
            d.setdefault("id", f"seed:{os.path.basename(path)}:{ln}")
            seeds.append(SimpleMaterial("seed", d))
    if not seeds:
        raise ValueError(f"seed file is empty: {path}")
    return seeds
