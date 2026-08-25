"""document_qa: document source — chunking → QA generation grounded in the
original text."""
from __future__ import annotations

from typing import Any, AsyncIterator

from corpuslab.config.loader import extract_json_object
from corpuslab.core.registry import register_strategy
from corpuslab.core.sample import TaskSpec, derive_id
from corpuslab.sources import load_documents, semantic_chunks, structure_chunks
from corpuslab.strategies.base import PlanExecuteStrategy

_CTX_REFS = ("根据上文", "根据给定文档", "如上文所述", "as mentioned above",
             "according to the passage", "per the document", "based on the text")


@register_strategy("document_qa")
class DocumentQAStrategy(PlanExecuteStrategy):
    type = "document_qa"

    async def _plan(self, materials: AsyncIterator[Any], budget: int,
                    ctx: Any) -> AsyncIterator[TaskSpec]:
        docs = load_documents(self.cfg.document_file, self.cfg.field_map)
        ch = self.cfg.chunking
        n = 0
        for doc in docs:
            text = doc.payload["text"]
            ranges = [(0, len(text))]
            if ch.enabled:
                ranges = (await semantic_chunks(text, ch, ctx) if ch.mode == "semantic"
                          else structure_chunks(text, ch.min_chunk_length,
                                                ch.max_chunk_length))
            for (start, end) in ranges:
                if n >= budget:
                    return
                chunk_text = text[start:end].strip()
                if not chunk_text:
                    continue
                sid = derive_id("docqa", doc.payload["id"], start, end)
                lineage = {"source": "document", "source_id": doc.payload["id"],
                           "chunk": [start, end]}
                yield TaskSpec(id=sid, strategy=self.type,
                               payload={"chunk": chunk_text,
                                        "doc_meta": doc.payload.get("meta", {})},
                               lineage=lineage)
                n += 1

    async def _execute_one(self, spec: TaskSpec, ctx: Any):
        chunk = spec.payload["chunk"]
        obj = extract_json_object(await ctx.chat(
            [{"role": "system",
              "content": "You write QA pairs grounded strictly in the given text. "
                         "The question must be self-contained (readable without "
                         "seeing the text)."},
             {"role": "user",
              "content": f"Source text:\n{chunk}\n\n"
                         'Return JSON: {"instruction": "...", "output": "..."}'}]))
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
        output = str(obj.get("output") or "")
        if len(output) > self.cfg.max_output_length:
            ctx.report.drop(self.type, "output_too_long")
            return None
        return self.make_sample(spec, instruction=instr, output=output,
                                extra_meta={"source_text": chunk})
