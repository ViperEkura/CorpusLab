"""YAML → config object: parsing, alias hints, format derivation, state
fingerprint.

The §10 resolution rules are the complete list of implicit behaviors:
endpoint merge, format derivation, env fallback.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import yaml
from pydantic import ValidationError

from corpuslab.config import schema as S

# Alias hints at top level (not legal keys; migration suggestions only)
_TOP_ALIASES = {"random_seed": "run.seed", "dry_run": "run.preview", "count": "plan.count",
                "api": "llm", "scoring": "judge + endpoints", "quality": "pipeline",
                "cleaner": "pipeline + embedding"}


class ConfigError(ValueError):
    pass


def _alias_hints(data: dict) -> list:
    hints = []
    for k in data:
        if k in _TOP_ALIASES:
            hints.append(f"section `{k}` has been merged into `{_TOP_ALIASES[k]}`")
    for st in data.get("strategies", []) or []:
        if isinstance(st, dict):
            for k in st:
                if k in S.ALIASES:
                    hints.append(f"strategy `{st.get('type')}`: `{k}` → `{S.ALIASES[k]}`")
    return hints


def load(path: str) -> S.Config:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: config root must be a mapping")
    hints = _alias_hints(raw)
    try:
        cfg = S.Config.model_validate(raw)
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "<root>"
            msgs.append(f"  {loc}: {err['msg']}")
        raise ConfigError("config validation failed:\n" + "\n".join(msgs)
                          + ("\nmigration hints: " + "; ".join(hints) if hints else "")) from e
    # Endpoint diff sections may declare only diff fields (model may be empty;
    # inheritance from llm happens field-by-field at resolve time)
    for name, ep in cfg.endpoints.items():
        ep.model = ep.model or cfg.llm.model
    return cfg


def resolve_endpoint(cfg: S.Config, name: Optional[str]) -> S.LlmCfg:
    """§10.1: fields not declared in endpoints.<name> inherit from llm
    field-by-field (never a whole-section replace)."""
    base = cfg.llm
    if name and name != "llm" and name in cfg.endpoints:
        diff = cfg.endpoints[name]
        merged = base.model_copy(update={
            "model": diff.model or base.model,
            "api_key": diff.api_key or base.api_key,
            "base_url": diff.base_url or base.base_url,
            "lang": diff.lang or base.lang,
            "concurrency": diff.concurrency,
            "params": {**base.params, **diff.params},
            "retry": diff.retry,
            "breaker": diff.breaker,
        })
        return merged
    return base


def env_resolve_endpoint(ep: S.LlmCfg) -> S.LlmCfg:
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


def env_resolve_embedding(cfg: S.Config) -> S.EmbeddingCfg:
    emb = cfg.embedding
    kw = {}
    if not emb.api_key:
        kw["api_key"] = os.environ.get("EMBEDDING_API_KEY")
    if not emb.base_url:
        kw["base_url"] = os.environ.get("EMBEDDING_BASE_URL")
    return emb.model_copy(update=kw) if kw else emb


def derive_format(cfg: S.Config) -> str:
    """§10.2 format derivation: explicit declaration wins; tool_call → openai;
    otherwise alpaca."""
    if cfg.output and cfg.output.format:
        return cfg.output.format
    has_tool = any(getattr(s, "type", "") == "tool_call" for s in cfg.strategies)
    return "openai" if has_tool else "alpaca"


def effective_output_path(cfg: S.Config) -> str:
    """With storage.type=duckdb the path is the database file (default
    extension .duckdb)."""
    assert cfg.output is not None
    path = cfg.output.path
    if cfg.output.storage.type == "duckdb" and not path.endswith(".duckdb"):
        return path + ".duckdb"
    return path


def dumps_state_relevant(cfg: S.Config) -> str:
    """Fingerprint material of state-relevant config (plan/preview excluded —
    changing them keeps resume compatible)."""
    def stage_dict(s: Any) -> dict:
        return {"type": s.type, **{k: v for k, v in s.model_dump().items() if k != "type"}}

    material = {
        "llm": {"model": cfg.llm.model, "lang": cfg.llm.lang},
        "embedding_model": cfg.embedding.model,
        "pipeline": [stage_dict(s) for s in cfg.pipeline],
        "strategies": [
            {"type": s.type,
             **{k: v for k, v in s.model_dump().items()
                if k in ("topics", "dimensions", "max_rounds", "depth_rate",
                         "branch_factor", "ratio_bounds", "evolution", "chunking",
                         "tools", "seed_file", "document_file", "example_num")}}
            for s in cfg.strategies
        ],
        "judge_dims": [d.name for d in cfg.judge.dimensions],
    }
    return json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)


def extract_json_object(text: str) -> Optional[dict]:
    """Extract the first JSON object from an LLM reply (shared by strategies
    and judges)."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    v = json.loads(text[start:i + 1])
                    if isinstance(v, dict):
                        return v
                except json.JSONDecodeError:
                    start = -1
    return None
