"""deep_thinking: concept source + mandatory reasoning (always emits
reasoning + output)."""
from __future__ import annotations

from typing import Any

from corpuslab.config.loader import extract_json_object
from corpuslab.core.registry import register_strategy
from corpuslab.core.sample import TaskSpec
from corpuslab.strategies.topic_driven import TopicDrivenStrategy, LANG_HINT

_SYS = ("You are an expert data author. Write one high-quality sample that "
        "includes explicit step-by-step reasoning, then the final answer.")


@register_strategy("deep_thinking")
class DeepThinkingStrategy(TopicDrivenStrategy):
    type = "deep_thinking"

    async def _execute_one(self, spec: TaskSpec, ctx: Any):
        p = spec.payload
        topic, slots = p["topic"], p["slots"]
        slot_desc = "; ".join(f"{k}: {v}" for k, v in slots.items()) or "free"
        user = (f"Topic: {topic['topic']}\nSlots: {slot_desc}\n"
                + (f"Knowledge background: {topic.get('knowledge')}\n"
                   if topic.get("knowledge") else "")
                + f"{LANG_HINT.get(ctx.lang, '')}\n"
                + 'Return JSON: {"instruction": "...", "reasoning": "...", '
                  '"output": "..."}')
        obj = extract_json_object(await ctx.chat(
            [{"role": "system", "content": _SYS}, {"role": "user", "content": user}]))
        if not obj or not obj.get("instruction"):
            return None
        return self.make_sample(spec, instruction=str(obj["instruction"]),
                                output=str(obj.get("output") or ""),
                                reasoning=str(obj.get("reasoning") or ""))
