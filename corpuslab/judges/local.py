"""Local scorer: fasttext coherence/fluency (optional extra — referencing it
without the dependency raises a clear error)."""
from __future__ import annotations

from typing import Any

from corpuslab.core.sample import Sample, Score


class FastTextScorer:
    """Emits [0,1] values for its configured dimensions (coherence /
    fluency / diversity) using a fasttext language model's average log
    probability normalized to [0,1]."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.dimensions = cfg.dimensions or ["coherence"]
        if not cfg.model_path:
            raise ValueError("fasttext scorer requires model_path")
        try:
            import fasttext  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "fasttext is not installed; `pip install corpuslab[fasttext]` "
                "or remove the scorer") from e
        self.model = fasttext.load_model(cfg.model_path)

    async def score(self, sample: Sample, ctx: Any) -> Score:
        text = sample.core_text().replace("\n", " ")
        if not text.strip():
            return Score(sample_id=sample.id, source="fasttext")
        labels, probs = self.model.predict(text, k=1)
        # P(next token) proxy: use label probability as fluency; coherence
        # blends it with length-normalized confidence
        p = float(probs[0]) if probs else 0.0
        length_factor = min(len(text) / 200.0, 1.0)
        out = {}
        for dim in self.dimensions:
            if dim == "coherence":
                out[dim] = round(0.5 * p + 0.5 * length_factor, 4)
            elif dim == "fluency":
                out[dim] = round(p, 4)
            else:                                # diversity etc.: neutral 0.5
                out[dim] = 0.5
        return Score(sample_id=sample.id, scores=out,
                     total=sum(out.values()), source="fasttext")
