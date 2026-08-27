"""tools: tool spec (OpenAI function schema) validation and loading."""
from __future__ import annotations

__all__ = ["load_tools"]

from typing import List

from corpuslab.sources.topics import SimpleMaterial


def load_tools(tools: List[dict]) -> list:
    """Validate OpenAI function tools: type=function with function.name and
    parameters present."""
    out = []
    for i, t in enumerate(tools):
        fn = t.get("function") if isinstance(t, dict) else None
        if not fn or not fn.get("name") or "parameters" not in fn:
            raise ValueError(f"tools[{i}] is not a valid OpenAI function definition")
        out.append(SimpleMaterial("tool", {"tool": t, "fn": fn}))
    if not out:
        raise ValueError("tools is empty")
    return out
