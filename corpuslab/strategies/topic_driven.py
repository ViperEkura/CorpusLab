"""topic_driven: concept source — slot cartesian sampling + knowledge injection."""
from __future__ import annotations

import itertools
from typing import Any, AsyncIterator

from corpuslab.config.loader import extract_json_object
from corpuslab.core.registry import register_strategy
from corpuslab.core.sample import TaskSpec, derive_id
from corpuslab.sources import load_topics
from corpuslab.strategies.base import PlanExecuteStrategy

LANG_HINT = {"zh": "Please answer in Chinese.", "en": "Answer in English."}

_SYS = ("You are an expert data author. Write one high-quality "
        "instruction-following sample.")


@register_strategy("topic_driven")
class TopicDrivenStrategy(PlanExecuteStrategy):
    type = "topic_driven"

    async def _plan(self, materials: AsyncIterator[Any], budget: int,
                    ctx: Any) -> AsyncIterator[TaskSpec]:
        topics = load_topics(self.cfg)
        dims = [(d.name, d.vals) for d in self.cfg.dimensions if d.vals]
        combos = list(itertools.product(*[vals for _, vals in dims])) or [()]
        # Concept sources do not consume external materials (topics are the
        # material); the iterator is intentionally left unread.
        for n in range(budget):
            topic = topics[n % len(topics)]
            combo = combos[n % len(combos)]
            slots = {name: val for (name, _), val in zip(dims, combo)}
            sid = derive_id("topic", self.cfg.type, topic.payload["topic"],
                            *[f"{k}={v}" for k, v in sorted(slots.items())], n)
            lineage = {"source": "topic", "topic": topic.payload["topic"], **slots}
            yield TaskSpec(id=sid, strategy=self.type,
                           payload={"topic": topic.payload, "slots": slots, "n": n},
                           lineage=lineage)

    async def _execute_one(self, spec: TaskSpec, ctx: Any):
        p = spec.payload
        topic, slots = p["topic"], p["slots"]
        slot_desc = "; ".join(f"{k}: {v}" for k, v in slots.items()) or "free"
        knowledge = topic.get("knowledge")
        user = (f"Topic: {topic['topic']}\nSlots: {slot_desc}\n"
                + (f"Knowledge background: {knowledge}\n" if knowledge else "")
                + f"{LANG_HINT.get(ctx.lang, '')}\n"
                + 'Return JSON: {"instruction": "...", "output": "..."}')
        obj = extract_json_object(await ctx.chat(
            [{"role": "system", "content": _SYS},
             {"role": "user", "content": user}]))
        if not obj or not obj.get("instruction"):
            return None
        reasoning = str(obj.get("reasoning") or "") if self.cfg.require_reasoning else ""
        return self.make_sample(spec, instruction=str(obj["instruction"]),
                                output=str(obj.get("output") or ""),
                                reasoning=reasoning)
