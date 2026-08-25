"""stats stage (streaming): special-character ratio, repetition ratios,
n-gram diversity."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, AsyncIterator

from corpuslab.core.registry import register_stage
from corpuslab.core.sample import Sample

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def special_char_ratio(text: str) -> float:
    if not text:
        return 0.0
    special = sum(1 for ch in text if not ch.isalnum()
                  and not ch.isspace() and not re.match(r"[\u3000-\u303f\uff00-\uffef]", ch))
    return special / len(text)


def _tokens(text: str, unit: str) -> list:
    if unit == "word":
        return _WORD_RE.findall(text)
    return list(text)


def repetition_ratio(text: str, unit: str, kind: str) -> float:
    toks = _tokens(text, unit)
    if not toks:
        return 0.0
    if kind == "word":
        c = Counter(_WORD_RE.findall(text))
        if not c:
            return 0.0
        # proportion of tokens that belong to any over-repeated type
        return sum(n for n in c.values() if n > 1) / sum(c.values())
    # char: longest run / total
    longest = 1
    run = 1
    for i in range(1, len(toks)):
        run = run + 1 if toks[i] == toks[i - 1] else 1
        longest = max(longest, run)
    return longest / len(toks)


def ngram_diversity(text: str, n: int, unit: str) -> float:
    toks = _tokens(text, unit)
    if len(toks) < n:
        return 1.0
    grams = [tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)]
    return len(set(grams)) / len(grams)


@register_stage("stats", scheduling="streaming")
class StatsStage:
    type = "stats"

    def __init__(self, cfg: Any):
        self.cfg = cfg

    async def apply_stream(self, stream: AsyncIterator[Sample],
                           ctx: Any) -> AsyncIterator[Sample]:
        c = self.cfg
        async for s in stream:
            text = s.core_text()
            scr = special_char_ratio(text)
            if scr > c.max_special_char_ratio:
                ctx.drop(s, "stats", f"special_char_ratio:{scr:.2f}")
                continue
            crr = repetition_ratio(text, c.unit, "char")
            if crr > c.max_char_repetition:
                ctx.drop(s, "stats", f"char_repetition:{crr:.2f}")
                continue
            wrr = repetition_ratio(text, "word", "word")
            if wrr > c.max_word_repetition:
                ctx.drop(s, "stats", f"word_repetition:{wrr:.2f}")
                continue
            div = ngram_diversity(text, c.ngram_n, c.unit)
            if div < c.min_ngram_diversity:
                ctx.drop(s, "stats", f"ngram_diversity:{div:.2f}")
                continue
            s.metrics["stats"] = {"special_char_ratio": round(scr, 4),
                                  "ngram_diversity": round(div, 4)}
            s.kept_by("stats")
            yield s
