"""evol_instruct: multi-round depth/breadth evolution with chain lineage and
one id per round (resumable mid-evolution)."""
from __future__ import annotations

import random
from typing import Any, AsyncIterator, List

from corpuslab.core.registry import register_strategy
from corpuslab.core.sample import Sample, TaskSpec, derive_id
from corpuslab.strategies.base import PlanExecuteStrategy


@register_strategy("evol_instruct")
class EvolInstructStrategy(PlanExecuteStrategy):
    type = "evol_instruct"

    async def _plan(self, materials: AsyncIterator[Any], budget: int,
                    ctx: Any) -> AsyncIterator[TaskSpec]:
        seeds = [m async for m in materials]      # SeedSource stream (§5.1)
        if not seeds:
            raise ValueError("seed source yielded no seeds")
        rng = random.Random((ctx.cfg.run.seed or 0) + 7)
        # Seeds → round-by-round evolution; one deterministic id per round:
        # evol:{seed}:r{round}:{k}
        frontier: List[dict] = [
            {"seed_id": s.payload.get("id"),
             "instruction": s.payload.get("instruction", ""),
             "chain": [s.payload.get("instruction", "")]}
            for s in seeds
        ]
        if self.cfg.include_seeds:
            for f in frontier:
                sid = derive_id("evol", f["seed_id"], "r0", 0)
                yield TaskSpec(id=sid, strategy=self.type,
                               payload={"instruction": f["instruction"], "round": 0,
                                        "answer": True, "chain": list(f["chain"])},
                               lineage={"source": "seed", "seed_id": f["seed_id"],
                                        "evolution_round": 0})
        produced = 0
        for rnd in range(1, self.cfg.max_rounds + 1):
            next_frontier: List[dict] = []
            for f in frontier:
                # Depth evolution (probability depth_rate)
                if rng.random() < self.cfg.depth_rate:
                    mut = self.cfg.depth_mutations[
                        rng.randrange(len(self.cfg.depth_mutations))]
                    sid = derive_id("evol", f["seed_id"], f"r{rnd}", len(next_frontier))
                    produced += 1
                    yield TaskSpec(
                        id=sid, strategy=self.type,
                        payload={"instruction": f["instruction"], "round": rnd,
                                 "mutation": {"name": mut.name, "prompt": mut.prompt},
                                 "seed_id": f["seed_id"], "chain": list(f["chain"]),
                                 "answer": self.cfg.generate_output},
                        lineage={"source": "seed", "seed_id": f["seed_id"],
                                 "evolution_round": rnd, "mutation": mut.name})
                    next_frontier.append(f | {"pending": True})
                # Breadth branches. id = round-local monotonic index so the
                # same (seed, round) never collides across frontier entries.
                for b in range(max(self.cfg.branch_factor, 0)):
                    sid = derive_id("evol", f["seed_id"], f"r{rnd}",
                                    len(next_frontier))
                    produced += 1
                    yield TaskSpec(
                        id=sid, strategy=self.type,
                        payload={"instruction": f["instruction"], "round": rnd,
                                 "mutation": {"name": "breadth",
                                              "prompt": "Rewrite the instruction "
                                                        "to a different but "
                                                        "related task."},
                                 "seed_id": f["seed_id"], "chain": list(f["chain"]),
                                 "answer": self.cfg.generate_output},
                        lineage={"source": "seed", "seed_id": f["seed_id"],
                                 "evolution_round": rnd, "mutation": "breadth"})
                    next_frontier.append(f | {"pending": True})
                if rng.random() < 0.5:
                    next_frontier.append(f)
            # The evolution tree keeps only this round's products (simplified
            # model: depth-first expansion)
            frontier = [f for f in next_frontier if f.get("pending")] or frontier
            if produced >= budget * self.cfg.max_rounds:
                break

    async def _execute_one(self, spec: TaskSpec, ctx: Any):
        p = spec.payload
        old = p["instruction"]
        mut = p["mutation"]
        evolved_obj = await self._safe(ctx,
            [{"role": "system", "content": "You evolve instructions to be harder "
                                           "and more specific. Return JSON only."},
             {"role": "user", "content":
              f"Original instruction: {old}\n"
              f"Evolution operator [{mut['name']}]: {mut['prompt']}\n"
              'Return JSON: {"instruction": "..."}'}],
            params=self.phases_params("evolve", ctx))
        if not evolved_obj or not evolved_obj.get("instruction"):
            return None
        new_instr = str(evolved_obj["instruction"])
        # ratio_bounds gate: guards against both degeneration and bloat
        lo, hi = self.cfg.ratio_bounds
        if old and not (lo <= len(new_instr) / max(len(old), 1) <= hi):
            ctx.report.drop(self.type, "ratio_bounds")
            return None
        chain = list(p.get("chain") or []) + [new_instr]
        reasoning = ""
        output = ""
        if p.get("answer"):
            ans_obj = await self._safe(ctx,
                [{"role": "system", "content": "You answer the instruction well."},
                 {"role": "user", "content": new_instr}],
                params=self.phases_params("answer", ctx))
            if ans_obj:
                output = str(ans_obj.get("output") or ans_obj.get("answer") or "")
                reasoning = str(ans_obj.get("reasoning") or "")
        if self.cfg.require_reasoning and not reasoning:
            reasoning = "(no reasoning returned)"
        lineage = {"source": "seed", "seed_id": p.get("seed_id"),
                   "evolution_round": p.get("round"), "mutation": mut["name"],
                   "evolution_chain": chain}
        return Sample(id=spec.id, strategy=self.type, instruction=new_instr,
                      output=output, reasoning=reasoning,
                      metadata={"lineage": lineage})
