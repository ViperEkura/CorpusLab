"""Domain data classes: TaskSpec / Sample / Score — the sole circulating units of the pipeline.

`id` is derived at Plan time (deterministic) and acts as the idempotency key
for checkpointing (docs/checkpoint-design.md §3).
"""

from __future__ import annotations
__all__ = [
    "TaskSpec",
    "Sample",
    "Score",
    "RunReport",
    "derive_id",
    "FORMAT_VERSION",
]

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_id(*parts: Any) -> str:
    """Derive a deterministic id from slot coordinates (not a content hash:
    it must be computable before spending money on LLM calls)."""
    raw = "|".join(str(p) for p in parts)
    return _sha256(raw)[:16]


@dataclass
class TaskSpec:
    """Plan-stage product: a task sheet. Deterministic id, unique within budget."""

    id: str
    strategy: str
    payload: dict = field(default_factory=dict)      # strategy-specific: slots, refs…
    lineage: dict = field(default_factory=dict)       # preset lineage (→ Sample.metadata)

    def to_json(self) -> str:
        return json.dumps({"id": self.id, "strategy": self.strategy,
                           "payload": self.payload, "lineage": self.lineage}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TaskSpec":
        d = json.loads(raw)
        return cls(id=d["id"], strategy=d["strategy"], payload=d.get("payload", {}),
                   lineage=d.get("lineage", {}))


@dataclass
class Sample:
    """Canonical form is unique; alpaca/chatml/sharegpt/openai are Sink-side renderers."""

    id: str
    strategy: str
    instruction: str = ""
    output: str = ""
    reasoning: str = ""
    messages: list = field(default_factory=list)      # multi-turn / tool_call form
    tools: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)      # lineage / metrics

    # Derived
    @property
    def lineage(self) -> dict:
        return self.metadata.setdefault("lineage", {})

    @property
    def metrics(self) -> dict:
        return self.metadata.setdefault("metrics", {})

    def kept_by(self, stage: str) -> None:
        self.metrics.setdefault("kept_by", []).append(stage)

    def text_for_dedup(self) -> str:
        """Dedup scope: join messages if present, otherwise instruction+output."""
        if self.messages:
            return "\n".join(f"{m.get('role')}:{m.get('content')}" for m in self.messages)
        return f"{self.instruction}\n{self.output}"

    def core_text(self) -> str:
        """Stats/length scope (metadata excluded so lineage never skews statistics)."""
        return self.text_for_dedup()

    # Validation (data-format.md §2.2)
    def validate(self, raise_on_error: bool = True) -> list:
        """Enforce the internal-format constraints (C1–C4). Returns the list
        of problems; raises ValueError when raise_on_error and any exist."""
        problems: list = []
        if not isinstance(self.id, str) or not self.id:
            problems.append("id must be a non-empty string")
        if not isinstance(self.strategy, str) or not self.strategy:
            problems.append("strategy must be a non-empty string")
        has_msg = bool(self.messages)
        has_flat = bool(self.instruction) or bool(self.output)
        if has_msg == has_flat:
            problems.append(
                "exactly one form must exist: messages (multi-turn/tool_call) "
                "XOR instruction/output (single-turn); got both or neither")
        if not isinstance(self.metadata, dict):
            problems.append("metadata must be an object")
        else:
            for key in ("lineage", "metrics"):
                if key in self.metadata and not isinstance(self.metadata[key], dict):
                    problems.append(f"metadata.{key} must be an object")
        if raise_on_error and problems:
            raise ValueError(
                f"invalid Sample {self.id!r}: " + "; ".join(problems))
        return problems

    # Serialization
    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "strategy": self.strategy,
            "metadata": self.metadata,
        }
        if self.messages:
            d["messages"] = self.messages
            if self.tools:
                d["tools"] = self.tools
        else:
            d["instruction"] = self.instruction
            d["output"] = self.output
            if self.reasoning:
                d["reasoning"] = self.reasoning
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict, *, validate: bool = True) -> "Sample":
        md = dict(d.get("metadata") or {})
        # Legacy top-level lineage migrates in only when metadata is absent
        if "metadata" not in d:
            md.setdefault("lineage", d.get("lineage") or {})
        sample = cls(
            id=d["id"],
            strategy=d.get("strategy", ""),
            instruction=d.get("instruction", "") or "",
            output=d.get("output", "") or "",
            reasoning=d.get("reasoning", "") or "",
            messages=list(d.get("messages") or []),
            tools=list(d.get("tools") or []),
            metadata=md,
        )
        if validate:
            sample.validate()
        return sample

    @classmethod
    def from_json(cls, raw: str) -> "Sample":
        return cls.from_dict(json.loads(raw))

    def fingerprint(self) -> str:
        return _sha256(self.text_for_dedup())


FORMAT_VERSION = "1"


@dataclass
class Score:
    """Unified output protocol shared by remote Judges and local Scorers."""

    sample_id: str
    scores: dict = field(default_factory=dict)   # {dim: value}
    total: float = 0.0
    source: str = "llm"                          # llm | fasttext | mixed
    endpoint: str = ""                           # endpoint that produced it (cache key)


@dataclass
class RunReport:
    """Run report: per-stage drop counts (waterfall) + score/cost summary."""

    produced: int = 0                    # samples that reached the Sink
    dropped: dict = field(default_factory=dict)    # {stage: {reason: n}}
    totals: list = field(default_factory=list)     # total_score per scored sample
    llm_calls: int = 0
    retried: int = 0
    embed_calls: int = 0
    cache_hits: int = 0

    def drop(self, stage: str, reason: str) -> None:
        self.dropped.setdefault(stage, {}).setdefault(reason, 0)
        self.dropped[stage][reason] += 1

    def drop_total(self) -> int:
        return sum(n for reasons in self.dropped.values() for n in reasons.values())

    def waterfall(self) -> str:
        lines = []
        for stage, reasons in self.dropped.items():
            parts = ", ".join(f"{r}={n}" for r, n in sorted(reasons.items()))
            lines.append(f"  {stage}: {parts}")
        return "\n".join(lines) if lines else "  (no drops)"

    def summary(self) -> str:
        total_in = self.produced + self.drop_total()
        return (f"pipeline report: {total_in} in → {self.produced} out\n"
                f"{self.waterfall()}\n"
                f"llm_calls={self.llm_calls} retried={self.retried} "
                f"embed_calls={self.embed_calls} cache_hits={self.cache_hits}")
