"""Renderers (pure functions): canonical Sample → alpaca / chatml / sharegpt
/ openai, plus `<think>` rendering for reasoning."""
from __future__ import annotations


from corpuslab.core.sample import Sample


def _think_wrap(text: str, reasoning: str) -> str:
    if not reasoning:
        return text
    return f"<think>\n{reasoning}\n</think>\n\n{text}"


def render_alpaca(sample: Sample, *, thinking: bool = False) -> dict:
    instruction = sample.instruction
    output = sample.output
    if thinking and sample.reasoning:
        output = _think_wrap(output, sample.reasoning)
    d = {"instruction": instruction, "output": output}
    md = sample.metadata
    if md.get("lineage"):
        d["metadata"] = {"id": sample.id, "strategy": sample.strategy,
                         "lineage": md.get("lineage"),
                         "metrics": md.get("metrics", {})}
    else:
        d["metadata"] = {"id": sample.id, "strategy": sample.strategy}
    return d


def render_chatml(sample: Sample, *, thinking: bool = False) -> dict:
    if sample.messages:
        msgs = [dict(m) for m in sample.messages]
        if thinking and sample.reasoning and msgs:
            last = msgs[-1]
            if last.get("role") == "assistant":
                last["content"] = _think_wrap(last.get("content") or "",
                                              sample.reasoning)
    else:
        msgs = []
        if sample.instruction:
            msgs.append({"role": "user", "content": sample.instruction})
        content = sample.output
        if thinking and sample.reasoning:
            content = _think_wrap(content, sample.reasoning)
        msgs.append({"role": "assistant", "content": content})
    return {"messages": msgs,
            "metadata": {"id": sample.id, "strategy": sample.strategy,
                         "lineage": sample.metadata.get("lineage", {}),
                         "metrics": sample.metadata.get("metrics", {})}}


def render_sharegpt(sample: Sample, *, thinking: bool = False) -> dict:
    role_map = {"user": "human", "assistant": "gpt", "system": "system"}
    convs = []
    if sample.messages:
        for m in sample.messages:
            convs.append({"from": role_map.get(m.get("role"), "user"),
                          "value": m.get("content") or ""})
    else:
        if sample.instruction:
            convs.append({"from": "human", "value": sample.instruction})
        content = sample.output
        if thinking and sample.reasoning:
            content = _think_wrap(content, sample.reasoning)
        convs.append({"from": "gpt", "value": content})
    return {"conversations": convs,
            "metadata": {"id": sample.id, "strategy": sample.strategy}}


def render_openai(sample: Sample, *, thinking: bool = False) -> dict:
    if sample.messages:
        msgs = [dict(m) for m in sample.messages]
        if thinking and sample.reasoning and msgs \
                and msgs[-1].get("role") == "assistant":
            msgs[-1]["content"] = _think_wrap(
                msgs[-1].get("content") or "", sample.reasoning)
    else:
        msgs = [{"role": "user", "content": sample.instruction}]
        content = sample.output
        if thinking and sample.reasoning:
            content = _think_wrap(content, sample.reasoning)
        msgs.append({"role": "assistant", "content": content})
    d = {"messages": msgs}
    if sample.tools:
        d["tools"] = sample.tools
    d["metadata"] = {"id": sample.id, "strategy": sample.strategy}
    return d


RENDERERS = {
    "alpaca": render_alpaca,
    "chatml": render_chatml,
    "sharegpt": render_sharegpt,
    "openai": render_openai,
}


def render(sample: Sample, fmt: str, *, thinking: bool = False) -> dict:
    fn = RENDERERS.get(fmt)
    if fn is None:
        raise ValueError(f"unknown output format: {fmt!r} "
                         f"(available: {sorted(RENDERERS)})")
    return fn(sample, thinking=thinking)
