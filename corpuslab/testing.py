"""Test doubles: deterministic FakeLLM / FakeEmbedding sharing the exact
production interfaces (chat / embed).

Enabled via env vars (consumed by engine._make_llm / _make_embedding):
    CORPUSLAB_FAKE_LLM=1
    CORPUSLAB_FAKE_EMBED=1
produces deterministic content keyed by the prompt so runs are reproducible
and dedup/judge behavior is observable without network access."""
from __future__ import annotations

import hashlib
import math
from typing import Optional


def _stable(prompt: str) -> int:
    return int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12], 16)


class FakeLLM:
    """Deterministic content generator: instruction/output JSON for generation
    prompts, score JSON for judge prompts."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages: list, *, endpoint: Optional[str] = None,
                   params: Optional[dict] = None) -> str:
        self.calls += 1
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        h = _stable(user + (endpoint or ""))

        # Judge prompt: "Score the sample" → emit scores JSON
        if user.startswith("Score the sample"):
            import json
            dims = {}
            seg = user.split("Dimensions: ", 1)
            if len(seg) == 2:
                for part in seg[1].split(";"):
                    name = part.strip().split(" ")[0].strip()
                    if name:
                        # deterministic 7..10 with endpoint flavor
                        dims[name] = 7 + (h >> (len(name) % 8)) % 4
            return json.dumps({"scores": dims})

        # Generation prompt: emit instruction/output keyed to the prompt
        import json
        variety = h % 97
        vocab = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta",
                 "theta", "iota", "kappa", "lambda", "mu", "nu", "xi"]
        words = [vocab[(h >> i) % len(vocab)] for i in range(8 + variety % 9)]
        return json.dumps({
            "instruction": f"Fake task #{h % 100000} (variety {variety}) about "
                           f"{user[:40]!r}",
            "output": " ".join(f"{w}-{h % 89}:" for w in words)
                      + " a deterministic synthesized answer body.",
            "reasoning": f"Step 1..Step {1 + variety % 3} reasoned deterministically.",
        })


class FakeEmbedding:
    """Deterministic embedding vectors: hashed bag-of-characters projection."""

    def __init__(self, dims: int = 32):
        self.dims = dims
        self.calls = 0

    def _vec(self, text: str) -> list:
        v = [0.0] * self.dims
        for ch in text.encode("utf-8"):
            v[(ch * 7 + len(text)) % self.dims] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [round(x / norm, 6) for x in v]

    async def embed(self, texts: list) -> list:
        self.calls += len(texts)
        return [self._vec(t) for t in texts]
