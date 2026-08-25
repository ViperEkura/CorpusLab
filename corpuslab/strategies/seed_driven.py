"""seed_driven: sample source — few-shot / crossover / mutation roulette."""
from __future__ import annotations

import random
from typing import Any, AsyncIterator

from corpuslab.config.loader import extract_json_object
from corpuslab.core.registry import register_strategy
from corpuslab.core.sample import TaskSpec, derive_id
from corpuslab.sources import load_seeds
from corpuslab.strategies.base import PlanExecuteStrategy


@register_strategy("seed_driven")
class SeedDrivenStrategy(PlanExecuteStrategy):
    type = "seed_driven"

    def _roulette(self, rng: random.Random):
        evo = self.cfg.evolution
        c, m = evo.crossover, evo.mutate
        total = c + m
        if total > 1.0:                        # auto-normalize (validate warns)
            c, m = c / total, m / total
            total = 1.0
        r = rng.random()
        if r < c:
            return "crossover"
        if r < total and m > 0:
            return "mutate"
        return "fewshot"

    async def _plan(self, materials: AsyncIterator[Any], budget: int,
                    ctx: Any) -> AsyncIterator[TaskSpec]:
        seeds = load_seeds(self.cfg.seed_file, self.cfg.field_map)
        # NOTE: never use builtin hash() here — it is randomized per process
        # (PYTHONHASHSEED) and would break id determinism across resume runs.
        # Use a stable digest-derived offset.
        offset = int(derive_id("rng", self.type), 16) % 10007
        rng = random.Random((ctx.cfg.run.seed or 0) + offset)
        for n in range(budget):
            op = self._roulette(rng)
            a = seeds[rng.randrange(len(seeds))]
            b = seeds[rng.randrange(len(seeds))] if op == "crossover" else None
            mutation = None
            if op == "mutate" and self.cfg.evolution.mutations:
                mutation = self.cfg.evolution.mutations[
                    rng.randrange(len(self.cfg.evolution.mutations))]
            sid = derive_id("seed", a.payload.get("id"), op, n)
            lineage = {"source": "seed", "seed_id": a.payload.get("id"),
                       "operator": op}
            if b is not None:
                lineage["seed_id_b"] = b.payload.get("id")
            if mutation:
                lineage["mutation"] = mutation.get("name")
            yield TaskSpec(id=sid, strategy=self.type,
                           payload={"a": a.payload, "b": b.payload if b else None,
                                    "op": op, "mutation": mutation, "n": n},
                           lineage=lineage)

    async def _execute_one(self, spec: TaskSpec, ctx: Any):
        p = spec.payload
        a, op, mutation = p["a"], p["op"], p["mutation"]
        examples = [a]
        if op == "crossover" and p.get("b"):
            examples = [a, p["b"]]
        shot = "\n\n".join(f"Example {i + 1}:\ninstruction: {e.get('instruction')}\n"
                           f"output: {e.get('output')}" for i, e in enumerate(examples))
        task = {
            "crossover": ("Combine example A's instruction with example B's output "
                          "style into one new sample."),
            "fewshot": "Write one new sample in the same style as the examples.",
        }
        extra = ""
        if op == "mutate" and mutation:
            value = ""
            if mutation.get("values"):
                value = ctx.rng.choice(mutation["values"])
            extra = "\nMutation requirement: " + \
                mutation["prompt"].replace("{value}", str(value))
        user = (f"{shot}\n\nTask: {task.get(op, task['fewshot'])}{extra}\n"
                'Return JSON: {"instruction": "...", "output": "..."}')
        obj = extract_json_object(await ctx.chat(
            [{"role": "system", "content": "You are a careful data augmenter."},
             {"role": "user", "content": user}]))
        if not obj or not obj.get("instruction"):
            return None
        return self.make_sample(spec, instruction=str(obj["instruction"]),
                                output=str(obj.get("output") or ""))
