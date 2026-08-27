"""Loader tests: env fallback, endpoint resolution, format derivation
(docs/project-structure.md §9)."""
from __future__ import annotations

import corpuslab.config.schema as S
import yaml

from corpuslab.config.loader import derive_format, load, resolve_endpoint
from tests.conftest import make_config


def test_format_derivation(tmp_path):
    cfg = make_config(tmp_path)                       # no tool_call → alpaca
    assert derive_format(cfg) == "alpaca"
    cfg.output.format = "chatml"
    assert derive_format(cfg) == "chatml"             # explicit wins


def test_format_derivation_tool_call(tmp_path):
    tools = [{
        "type": "function",
        "function": {"name": "get_weather",
                     "description": "Get weather",
                     "parameters": {"type": "object", "properties": {}}},
    }]
    cfg = make_config(tmp_path, strategies_yaml=[
        {"type": "tool_call", "weight": 1.0, "tools": tools},
    ])
    assert derive_format(cfg) == "openai"


def test_endpoint_resolution_merges_fields(tmp_path):
    cfg = make_config(tmp_path)
    ep = S.LlmCfg(model="m-pro", concurrency=4)
    cfg.endpoints["pro"] = ep
    resolved = resolve_endpoint(cfg, "pro")
    assert resolved.model == "m-pro"                  # diff declared
    assert resolved.concurrency == 4                  # diff declared
    assert resolved.lang == cfg.llm.lang              # inherited from llm
    assert resolve_endpoint(cfg, "llm").model == "fake-model"


def test_env_fallback_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "sk-test")
    cfg = make_config(tmp_path)
    from corpuslab.llm.endpoints import env_fallback_llm
    resolved = env_fallback_llm(cfg.llm)
    assert resolved.api_key == "sk-test"


def test_env_fallback_explicit_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "sk-env")
    cfg = make_config(tmp_path)
    cfg.llm.api_key = "sk-explicit"
    from corpuslab.llm.endpoints import env_fallback_llm
    assert env_fallback_llm(cfg.llm).api_key == "sk-explicit"


def test_embedding_never_falls_back_to_llm_key(monkeypatch, tmp_path):
    # config-design.md §10.4: prevents leaking keys to a third-party endpoint
    monkeypatch.setenv("API_KEY", "sk-llm")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    cfg = make_config(tmp_path)
    from corpuslab.llm.endpoints import env_resolve_embedding
    emb = env_resolve_embedding(cfg)
    assert emb.api_key is None


def test_dotenv_autoload_no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "sk-already-set")
    (tmp_path / ".env").write_text('API_KEY="sk-from-dotenv"\n', encoding="utf-8")
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.dump({"llm": {"model": "m"}}), encoding="utf-8")
    load(str(cfg_path))                               # must not raise
    import os
    assert os.environ["API_KEY"] == "sk-already-set"  # existing env wins


def test_json_extraction_from_fence():
    from corpuslab.config.loader import extract_json_object
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('prefix {"a": {"b": 2}} suffix') == {"a": {"b": 2}}
    assert extract_json_object("no json here") is None
