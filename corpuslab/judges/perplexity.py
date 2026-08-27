"""Perplexity scorer (route A: OpenAI-compatible API logprobs).

Two modes, chosen by `mode`:

- **teacher_forced** — the true PPL. Requires an endpoint that echoes
  `prompt_logprobs` on POST {base}/completions (vLLM / SGLang self-hosted).
  The whole sample text is placed in the prompt; the mean token NLL of the
  prompt tokens yields exact perplexity. Endpoints that silently ignore
  `prompt_logprobs` are detected and raise a clear error.

- **continuation** — a surrogate for gateways without prompt echo (e.g.
  DeepSeek official). The text is split at `target_ratio`: prefix conditions
  the model, and the greedy continuation's mean token NLL is measured.
  Lower NLL = more predictable/probable prose; it correlates with PPL well
  enough for filtering, but is NOT mathematically PPL.

Output is normalized to [0,1] as `ppl_quality = clamp(1 - nll/ceiling)`:
natural prose ≈ 0.6-0.9, gibberish near 0. Written into same-named
dimensions like any local scorer (aggregate.py semantics)."""
from __future__ import annotations

__all__ = ["PerplexityScorer", "parse_prompt_logprobs", "mean_token_nll",
           "ppl_to_quality"]

import math
from typing import Any, List, Optional

import httpx

from corpuslab.core.sample import Sample, Score


def parse_prompt_logprobs(payload: dict) -> Optional[List[Optional[float]]]:
    """Extract vLLM-style `prompt_logprobs` from a /completions response.

    Each element is either None (first token) or a dict {token_id_or_str:
    logprob}; returns the chosen-token logprob per position, or None when the
    endpoint did not honor the parameter."""
    try:
        choice = payload["choices"][0]
        plp = choice["logprobs"].get("prompt_logprobs")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    if not isinstance(plp, list) or not plp:
        return None
    out: List[Optional[float]] = []
    for item in plp:
        if item is None:
            out.append(None)
        elif isinstance(item, dict):
            # dict maps token-id/str → logprob; exactly one entry per position
            out.append(next(iter(item.values())))
        else:
            out.append(None)
    return out


def mean_token_nll(logprobs: List[Optional[float]]) -> Optional[float]:
    """Mean negative log-likelihood over scored tokens (None positions skipped)."""
    vals = [-lp for lp in logprobs if lp is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def ppl_to_quality(nll: float, ceiling: float = 6.0) -> float:
    """Map mean NLL to [0,1]; nll >= ceiling → 0, nll <= 1 → ~0.83+."""
    if not math.isfinite(nll):
        return 0.0
    q = 1.0 - max(nll, 0.0) / max(ceiling, 1e-9)
    return round(min(max(q, 0.0), 1.0), 4)


class PerplexityScorer:
    """API-backed perplexity scorer conforming to the Judge protocol.

    Endpoint identity resolution happens in engine.build_judge (llm fallback +
    env); the resolved model/base_url/api_key are injected here."""

    def __init__(self, cfg: Any, *, model: Optional[str] = None,
                 base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.cfg = cfg
        self.mode = getattr(cfg, "mode", "continuation")
        if self.mode not in ("teacher_forced", "continuation"):
            raise ValueError(
                f"perplexity mode must be teacher_forced|continuation, "
                f"got {self.mode!r}")
        self.model = model or cfg.model
        self.base_url = base_url or cfg.base_url
        self.api_key = api_key if api_key is not None else cfg.api_key
        self.dimensions = list(cfg.dimensions or ["ppl_quality"])
        self.ceiling = float(getattr(cfg, "nll_ceiling", 6.0))
        self.max_chars = int(getattr(cfg, "max_chars", 4000))
        self.target_ratio = float(getattr(cfg, "target_ratio", 0.5))
        self.max_tokens = int(getattr(cfg, "max_target_tokens", 256))
        if not self.model or not self.base_url:
            raise ValueError(
                "perplexity scorer needs a model and base_url (declare "
                "judge.perplexity.{model,base_url} or set llm section)")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _post_completions(self, client: httpx.AsyncClient,
                                body: dict) -> dict:
        r = await client.post(self.base_url.rstrip("/") + "/completions",
                              json=body, headers=self._headers())
        r.raise_for_status()
        return r.json()

    # ── teacher_forced ────────────────────────────────────
    async def _score_teacher_forced(self, text: str) -> float:
        body = {"model": self.model, "prompt": text,
                "max_tokens": 1, "echo": False, "logprobs": 1,
                "prompt_logprobs": 0}
        async with httpx.AsyncClient(timeout=120) as client:
            data = await self._post_completions(client, body)
        plp = parse_prompt_logprobs(data)
        if plp is None:
            raise RuntimeError(
                "perplexity mode=teacher_forced requires the endpoint to "
                "return prompt_logprobs (vLLM/SGLang); this gateway ignored "
                "it. Use mode=continuation instead.")
        # first position has no context → always None, skip via mean_token_nll
        nll = mean_token_nll(plp)
        if nll is None:
            raise RuntimeError("endpoint returned no usable prompt logprobs")
        return nll

    # ── continuation ──────────────────────────────────────
    async def _score_continuation(self, text: str) -> float:
        cut = max(16, int(len(text) * self.target_ratio))
        ctx_text, target_hint = text[:cut], text[cut:]
        body = {"model": self.model, "prompt": ctx_text,
                "max_tokens": min(self.max_tokens, max(8, len(target_hint) // 2 + 8)),
                "temperature": 0.0, "top_p": 1.0, "logprobs": 0}
        async with httpx.AsyncClient(timeout=120) as client:
            data = await self._post_completions(client, body)
        try:
            choice = data["choices"][0]
            lp = choice["logprobs"] or {}
            nlp = [x for x in (lp.get("token_logprobs") or []) if x is not None]
        except (KeyError, IndexError, TypeError):
            nlp = []
        if not nlp:
            raise RuntimeError(
                "perplexity mode=continuation got no completion logprobs "
                "(endpoint must support logprobs on /completions)")
        return sum(-x for x in nlp) / len(nlp)

    # ── Judge protocol ────────────────────────────────────
    async def score(self, sample: Sample, ctx: Any) -> Score:
        text = sample.core_text()[:self.max_chars]
        try:
            if self.mode == "teacher_forced":
                nll = await self._score_teacher_forced(text)
            else:
                nll = await self._score_continuation(text)
        except Exception as e:
            # scorer outage is not evidence the sample is bad: return no
            # score; aggregate treats it as an absent dimension
            ctx.report.drop("perplexity", f"scorer_error:{type(e).__name__}")
            return Score(sample_id=sample.id, source="perplexity")

        quality = ppl_to_quality(nll, self.ceiling)
        scores = {}
        for dim in self.dimensions:
            scores[dim] = quality                  # broadcast to declared dims
        if not scores:
            scores["ppl_quality"] = quality
        return Score(sample_id=sample.id, scores=scores,
                     total=sum(scores.values()), source="perplexity")
