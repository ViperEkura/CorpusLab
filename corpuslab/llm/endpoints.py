"""Endpoint resolution cache (project-structure.md §3.7): resolve(name) →
ResolvedEndpoint, cached per client instance.

Resolution rules live here (moved out of config/loader): field-by-field
inheritance from `llm` + env fallback (config-design.md §10.1/§10.4).
"""
from __future__ import annotations

__all__ = [
    "ResolvedEndpoint",
    "EndpointResolver",
    "merge_endpoint",
    "env_fallback_llm",
    "env_resolve_embedding",
]

import os
from dataclasses import dataclass
from typing import Dict, Optional

from corpuslab.config.schema import Config, LlmCfg


def merge_endpoint(cfg: Config, name: Optional[str]) -> LlmCfg:
    """§10.1: fields not declared in endpoints.<name> inherit from llm
    field-by-field (never a whole-section replace)."""
    base = cfg.llm
    if name and name != "llm" and name in cfg.endpoints:
        diff = cfg.endpoints[name]
        return base.model_copy(update={
            "model": diff.model or base.model,
            "api_key": diff.api_key or base.api_key,
            "base_url": diff.base_url or base.base_url,
            "lang": diff.lang or base.lang,
            "concurrency": diff.concurrency,
            "params": {**base.params, **diff.params},
            "retry": diff.retry,
            "breaker": diff.breaker,
        })
    return base


def env_fallback_llm(ep: LlmCfg) -> LlmCfg:
    """§10.4 env fallback (explicit value wins). Embedding never falls back to
    llm credentials (avoids leaking keys to a third-party endpoint)."""
    kw = {}
    if not ep.api_key:
        kw["api_key"] = os.environ.get("API_KEY")
    if not ep.base_url:
        kw["base_url"] = os.environ.get("BASE_URL")
    if kw:
        return ep.model_copy(update=kw)
    return ep


def env_resolve_embedding(cfg: Config):
    """§10.4 embedding credential fallback (EMBEDDING_* vars only)."""
    emb = cfg.embedding
    kw = {}
    if not emb.api_key:
        kw["api_key"] = os.environ.get("EMBEDDING_API_KEY")
    if not emb.base_url:
        kw["base_url"] = os.environ.get("EMBEDDING_BASE_URL")
    return emb.model_copy(update=kw) if kw else emb


@dataclass(frozen=True)
class ResolvedEndpoint:
    """A fully resolved endpoint: its declared name (the identity shared by
    semaphore and breaker) plus the merged config."""
    name: str                                   # endpoint identity key
    cfg: LlmCfg                                 # merged + env-resolved config


class EndpointResolver:
    """Per-client cache: name → ResolvedEndpoint."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cache: Dict[str, ResolvedEndpoint] = {}

    def resolve(self, name: Optional[str]) -> ResolvedEndpoint:
        key = name or "llm"
        if key not in self._cache:
            merged = env_fallback_llm(merge_endpoint(self.cfg, name))
            self._cache[key] = ResolvedEndpoint(name=key, cfg=merged)
        return self._cache[key]
