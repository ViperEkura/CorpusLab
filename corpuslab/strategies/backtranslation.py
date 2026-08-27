"""instruction_backtranslation: document source + instruction inference —
**the source text is locked in as output** (prevents factual drift)."""
from __future__ import annotations

import random
from typing import Any, AsyncIterator

from corpuslab.core.registry import register_strategy
from corpuslab.core.sample import TaskSpec, derive_id
from corpuslab.sources.documents import normalize_text
from corpuslab.strategies.base import PlanExecuteStrategy

_CTX_REFS = ("根据上文", "根据给定文档", "如上文所述", "as mentioned above",
             "according to the passage", "per the document", "based on the text")


@register_strategy("instruction_backtranslation")
class BacktranslationStrategy(PlanExecuteStrategy):
    type = "instruction_backtranslation"

    async def _plan(self, materials: AsyncIterator[Any], budget: int,
                    ctx: Any) -> AsyncIterator[TaskSpec]:
        docs = [m async for m in materials]       # DocumentSource stream (§5.1)
        if not docs:
            raise ValueError("document source yielded no documents")
        if self.cfg.shuffle:
            rng = random.Random(ctx.cfg.run.seed or 0)
            rng.shuffle(docs)
        n = 0
        for doc in docs:
            if n >= budget:
                return
            text = normalize_text(str(doc.payload["text"]))
            if not (self.cfg.min_document_length <= len(text)
                    <= self.cfg.max_document_length):
                continue
            # Include the ordinal: two docs sharing an id AND a text length
            # must not collide.
            sid = derive_id("bt", doc.payload["id"], n, len(text))
            lineage = {"source": "document", "source_id": doc.payload["id"],
                       "chunk": [0, len(text)]}
            yield TaskSpec(id=sid, strategy=self.type,
                           payload={"text": text}, lineage=lineage)
            n += 1

    async def _execute_one(self, spec: TaskSpec, ctx: Any):
        text = spec.payload["text"]
        obj = await self._safe(ctx,
            [{"role": "system",
              "content": "You infer the instruction that the given text answers. "
                         "The instruction must be self-contained. Return JSON only."},
             {"role": "user",
              "content": f"Text:\n{text}\n\n"
                         'Return JSON: {"instruction": "..."}'}],
            params=self.phases_params("backtranslate", ctx))
        if not obj or not obj.get("instruction"):
            return None
        instr = str(obj["instruction"])
        if self.cfg.reject_context_references and \
                any(k in instr.lower() for k in _CTX_REFS):
            ctx.report.drop(self.type, "context_reference")
            return None
        if len(instr) > self.cfg.max_instruction_length:
            ctx.report.drop(self.type, "instruction_too_long")
            return None
        # Invariant: whatever output the model returned is discarded; the
        # final output is the normalized source text
        return self.make_sample(spec, instruction=instr, output=text)
