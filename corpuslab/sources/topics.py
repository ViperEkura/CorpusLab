"""topics: topic list material (weight normalization)."""
from __future__ import annotations

__all__ = ["SimpleMaterial", "load_topics"]

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SimpleMaterial:
    kind: str
    payload: dict = field(default_factory=dict)


def load_topics(strategy_cfg: Any) -> list:
    """Normalize weights (they declare ratios; the sum need not be 1)."""
    total = sum(max(t.weight, 0.0) for t in strategy_cfg.topics) or 1.0
    out = []
    for t in strategy_cfg.topics:
        p = {"topic": t.topic, "weight": max(t.weight, 0.0) / total}
        if t.knowledge:
            p["knowledge"] = t.knowledge
        out.append(SimpleMaterial("topic", p))
    return out
