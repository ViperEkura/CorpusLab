"""tool_call: spec source — trajectory skeleton → fill arguments / simulated
returns / final answer, with a strict validating parser (rejects unknown
functions, malformed argument JSON, duplicate call ids, broken chains)."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from corpuslab.core.registry import register_strategy
from corpuslab.core.sample import TaskSpec, derive_id
from corpuslab.strategies.base import PlanExecuteStrategy

_SYS = ("You generate realistic tool-calling conversations. Use only the "
        "provided functions. Return JSON only.")


def validate_trajectory(messages: list, tools: list) -> str | None:
    """Return a rejection reason, or None when the trajectory is valid.

    Parallel tool calls are valid OpenAI form: one assistant message may open
    several calls; each must get exactly one tool result (order-insensitive),
    and every opened call must be answered before the next plain assistant
    turn closes the block."""
    known = {t["function"]["name"] for t in tools if t.get("function")}
    seen_ids: set = set()
    pending_calls: list = []                 # opened but unanswered call ids

    for m in messages:
        role = m.get("role")
        if role == "assistant":
            calls = m.get("tool_calls") or []
            if pending_calls and not calls:
                return "broken_chain:no_result"
            for c in calls:
                cid = (c.get("id") or "").strip()
                fn = ((c.get("function") or {}).get("name") or "").strip()
                if not cid:
                    return "missing_call_id"
                if cid in seen_ids:
                    return "duplicate_call_id"
                seen_ids.add(cid)
                if fn not in known:
                    return f"unknown_function:{fn}"
                try:
                    json.loads((c["function"].get("arguments") or "{}"))
                except (json.JSONDecodeError, TypeError):
                    return "invalid_arguments_json"
                pending_calls.append(cid)
        elif role == "tool":
            cid = (m.get("tool_call_id") or "").strip()
            if cid not in pending_calls:
                return "broken_chain:orphan_result"
            # every result must answer a distinct call; the same id twice
            # would have hit duplicate handling only on assistant side
            seen_ids.add(cid)
            pending_calls.remove(cid)
    if pending_calls:
        return "broken_chain:no_result"
    return None


@register_strategy("tool_call")
class ToolCallStrategy(PlanExecuteStrategy):
    type = "tool_call"

    async def _plan(self, materials: AsyncIterator[Any], budget: int,
                    ctx: Any) -> AsyncIterator[TaskSpec]:
        tools = [m async for m in materials]      # ToolSource stream (§5.1)
        if not tools:
            raise ValueError("tool source yielded no tools")
        for n in range(budget):
            topic = self.cfg.topics[n % len(self.cfg.topics)]
            sid = derive_id("tool", topic, n)
            lineage = {"source": "tool", "topic": topic}
            yield TaskSpec(id=sid, strategy=self.type,
                           payload={"topic": topic, "n": n}, lineage=lineage)

    async def _execute_one(self, spec: TaskSpec, ctx: Any):
        tools = self.cfg.tools
        tool_desc = json.dumps(tools, ensure_ascii=False)
        system = self.cfg.system_prompt or (
            "You are a helpful assistant with access to the following tools. "
            "Use them when appropriate.")
        user = (f"Scenario topic: {spec.payload['topic']}\n"
                f"Available tools: {tool_desc}\n"
                f"Produce a realistic conversation with at most "
                f"{self.cfg.max_tool_calls_per_sample} tool call(s).\n"
                "Return JSON: {\"messages\": [{\"role\": \"...\", \"content\": \"...\", "
                "\"tool_calls\": [{\"id\": \"call_1\", \"type\": \"function\", "
                "\"function\": {\"name\": \"...\", \"arguments\": \"{...}\"}}], "
                "\"tool_call_id\": \"...\"}], ...}")
        obj = await self._safe(ctx, [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": user}])
        if not obj or not obj.get("messages"):
            return None
        messages = obj["messages"]
        # Normalize into OpenAI trajectory form: system first, tool roles kept
        norm = [{"role": "system", "content": system}]
        for m in messages:
            if m.get("role") == "system":
                continue
            norm.append(m)
        reason = validate_trajectory(norm, tools)
        if reason is not None:
            ctx.report.drop(self.type, f"invalid_trajectory:{reason}")
            return None
        return self.make_sample(spec, messages=norm, tools=tools)
